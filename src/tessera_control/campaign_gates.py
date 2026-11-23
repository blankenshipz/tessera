"""Conditional inter-campaign unlock and human gate decisions (ADR 0045 section 1).

A campaign (or a standing approval's next start) names an earlier campaign and is admitted
only if that campaign carries either a recorded human gate ``go`` or a platform-computed
paired-delta outcome in an allowed set. The check is a pure read inside the admission
transaction and fails closed: anything missing, stale, unresolved or unevaluable refuses.
A human gate decision is a human record: only a live human actor can write one, and no
agent, worker or external-scope credential can.
"""

from __future__ import annotations

import json
import math
import operator
import sqlite3
import uuid
from typing import Any

from pydantic import ValidationError

from .approval_trust import (
    CLASS_HUMAN_SESSION,
    CLASS_OWNER_AUTOMATION_LABELLED,
    CLASS_OWNER_DELEGATED,
    decision_provenance,
    require_decision_actor,
)
from .campaign_gate_models import MeasurementCondition, RecordGateDecision, UnlockRequirement
from .gate_evidence_paired import PairedRefused, compute_paired_delta, paired_outcome
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    canonical_digest,
    utc_now,
)

GATEABLE_STATUSES = ("succeeded", "stopped", "failed")

# Which recorded decision classes count as the human ``go`` of an unlock. A fresh install
# accepts only a person's session. An operator may additionally accept an owner credential
# that declared itself labelled automation (``--accept-automation-gate-go``); the decision
# row keeps its class, so the receipt never presents a script's go as a person's. The
# unverified desktop-owner label is never accepted.
_GO_CLASSES: frozenset[str] = frozenset({CLASS_HUMAN_SESSION})


def configure_go_authority(*, accept_automation_labelled: bool) -> None:
    """Operator opt-in, set once at service start; the default stays conservative."""
    global _GO_CLASSES
    _GO_CLASSES = frozenset(
        {CLASS_HUMAN_SESSION}
        | ({CLASS_OWNER_AUTOMATION_LABELLED} if accept_automation_labelled else set())
    )


def go_classes() -> frozenset[str]:
    return _GO_CLASSES


_OPS = {
    "lt": operator.lt,
    "le": operator.le,
    "gt": operator.gt,
    "ge": operator.ge,
    "eq": operator.eq,
}


def _measurement_refusal(
    connection: sqlite3.Connection, campaign_id: str, condition: MeasurementCondition
) -> str | None:
    """None when the condition holds on the campaign's immutable terminal receipt."""
    row = connection.execute(
        "SELECT receipt_json FROM campaign_receipts WHERE campaign_id=?", (campaign_id,)
    ).fetchone()
    if row is None:
        return "measurement_receipt_missing"
    try:
        receipt = json.loads(row["receipt_json"])
        stage = next(s for s in receipt["stages"] if s["stage_key"] == condition.stage)
    except (ValueError, KeyError, TypeError, StopIteration):
        return "measurement_stage_missing"
    succeeded = [a for a in stage.get("attempts", []) if a.get("status") == "succeeded"]
    if len(succeeded) != 1:
        return "measurement_attempt_not_unique"
    values = [
        m.get("value")
        for m in succeeded[0].get("measurements", [])
        if isinstance(m, dict) and m.get("name") == condition.name
    ]
    if len(values) != 1:
        return "measurement_not_recorded"
    value = values[0]
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        return "measurement_not_numeric"
    if not _OPS[condition.op](float(value), condition.value):
        return "measurement_condition_false"
    return None


def requirement_digest(requirement: UnlockRequirement) -> str:
    return canonical_digest(requirement.model_dump(mode="json"))


def parse_unlock_json(value: str | None) -> list[UnlockRequirement]:
    """The stored requirements of a standing approval; unparseable stored data raises
    ValueError, which callers treat as a refusal."""
    if not value:
        return []
    try:
        return [UnlockRequirement.model_validate(item) for item in json.loads(value)]
    except (ValueError, TypeError, ValidationError) as error:
        raise ValueError("stored unlock requirements are invalid") from error


def effective_decision(connection: sqlite3.Connection, campaign_id: str) -> sqlite3.Row | None:
    """The latest human gate decision of a campaign (a later row retracts an earlier one)."""
    return connection.execute(
        "SELECT * FROM campaign_gate_decisions WHERE campaign_id=? ORDER BY rowid DESC LIMIT 1",
        (campaign_id,),
    ).fetchone()


def _exposure_unresolved(connection: sqlite3.Connection, campaign: sqlite3.Row) -> bool:
    return (
        campaign["held_cents"] > 0
        or connection.execute(
            "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND status='unknown'",
            (campaign["id"],),
        ).fetchone()
        is not None
    )


def _evaluate_one(
    connection: sqlite3.Connection, org_id: str, project_id: str, requirement: UnlockRequirement
) -> tuple[dict[str, Any] | None, str | None]:
    """(satisfied basis, None) or (None, short refusal code).

    A stated measurement condition narrows the basis: both must hold.
    """
    basis, code = _evaluate_basis(connection, org_id, project_id, requirement)
    if basis is None or requirement.measurement is None:
        return basis, code
    refusal = _measurement_refusal(connection, requirement.campaign_id, requirement.measurement)
    if refusal is not None:
        return None, refusal
    return basis, None


def _evaluate_basis(
    connection: sqlite3.Connection, org_id: str, project_id: str, requirement: UnlockRequirement
) -> tuple[dict[str, Any] | None, str | None]:
    named = connection.execute(
        "SELECT * FROM campaigns WHERE id=? AND project_id=? AND org_id=?",
        (requirement.campaign_id, project_id, org_id),
    ).fetchone()
    if named is None:
        return None, "campaign_not_found"
    if named["status"] not in GATEABLE_STATUSES:
        return None, "campaign_not_finished"
    if _exposure_unresolved(connection, named):
        return None, "exposure_unresolved"
    decision = effective_decision(connection, named["id"])
    if decision is not None and decision["decision"] == "no_go":
        return None, "human_no_go"  # a human veto beats any computed outcome
    if decision is not None and requirement.accept_human_go:
        if decision["decision_class"] in _GO_CLASSES:
            return {"basis_kind": "human_gate_go", "gate_decision_id": decision["id"]}, None
        if decision["decision_class"] == CLASS_OWNER_DELEGATED and decision["delegation_id"]:
            # A go the platform recorded because an owner-signed delegation's written rule was
            # met (ADR 0122): its class and delegation stay on the row and in the evaluation.
            # The stored basis_kind stays "human_gate_go" (a schema CHECK); the receipt's unlock
            # section reports the decision's class and delegation beside it.
            return {"basis_kind": "human_gate_go", "gate_decision_id": decision["id"]}, None
        # An owner credential running a script (automation label) is not a person's go
        # unless the operator opted in (configure_go_authority).
        if requirement.bound_outcome is None:
            return None, "go_not_human_session"
    rule = requirement.bound_outcome
    if rule is None:
        return None, "no_human_go"
    try:
        result = compute_paired_delta(
            connection,
            project_id,
            arm_a_variant_id=rule.arm_a_variant_id,
            arm_b_variant_id=rule.arm_b_variant_id,
            lock_id=rule.split_lock_id,
            metric=rule.metric,
            partition=rule.partition,
            ci_method=rule.ci_method,
            campaign_id=named["id"],
        )
    except PairedRefused as error:
        return None, f"unevaluable_{error.code}"
    except NotFound:
        return None, "unevaluable_not_found"
    outcome, reason = paired_outcome(
        result,
        statistic=rule.statistic,
        comparison=rule.comparison,
        threshold=rule.threshold,
        minimum_pairs=rule.minimum_pairs,
        minimum_samples=rule.minimum_samples,
        allowed_sources=list(rule.allowed_sources),
    )
    if outcome is None:
        return None, f"unevaluable_{reason}"
    if outcome not in rule.allowed_outcomes:
        return None, f"outcome_{outcome}_not_allowed"
    return {
        "basis_kind": "bound_outcome",
        "outcome": outcome,
        "result_digest": result["result_digest"],
    }, None


def evaluate_unlock(
    connection: sqlite3.Connection,
    org_id: str,
    project_id: str,
    requirements: list[UnlockRequirement],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Every requirement must hold. Returns (refusal reason, evaluations to record)."""
    evaluations: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements):
        basis, code = _evaluate_one(connection, org_id, project_id, requirement)
        if basis is None:
            return f"unlock_not_satisfied:{index}:{code}"[:300], []
        evaluations.append(
            {
                "requirement_index": index,
                "requirement_digest": requirement_digest(requirement),
                "named_campaign_id": requirement.campaign_id,
                "gate_decision_id": basis.get("gate_decision_id"),
                "outcome": basis.get("outcome"),
                "result_digest": basis.get("result_digest"),
                "basis_kind": basis["basis_kind"],
            }
        )
    return None, evaluations


_STORED_INVALID = "Admission refused: unlock_not_satisfied:stored_requirements_invalid"


def readmission_requirements(
    connection: sqlite3.Connection,
    campaign: Any,
    supplied: list[UnlockRequirement] | None,
) -> list[UnlockRequirement]:
    """The unlock requirements a forward re-approval of ``campaign`` must re-evaluate.

    They are the first approval's, identified by the digests it recorded (no requirement
    text is stored for a human approval). A human re-approval restates them exactly; a
    campaign started by a standing approval takes them from that approval's immutable
    ``unlock_json`` unless restated. A missing, different, added or stricter set is refused
    (409), never dropped or silently swapped: a re-approval re-checks the conditions the
    campaign was admitted under, and only those."""
    first = connection.execute(
        """SELECT id FROM campaign_approvals WHERE campaign_id=?
        ORDER BY rowid LIMIT 1""",
        (campaign["id"],),
    ).fetchone()
    rows = (
        []
        if first is None
        else connection.execute(
            """SELECT requirement_index, requirement_digest, standing_approval_id
            FROM campaign_unlock_evaluations WHERE campaign_id=? AND approval_id=?
            ORDER BY requirement_index""",
            (campaign["id"], first["id"]),
        ).fetchall()
    )
    if not rows:
        if supplied:
            raise Conflict(
                "An unlock applies to a campaign's first approval only; this campaign's "
                "first approval had none."
            )
        return []
    if [row["requirement_index"] for row in rows] != list(range(len(rows))):
        raise Conflict(_STORED_INVALID)
    requirements = list(supplied or [])
    standing_id = rows[0]["standing_approval_id"]
    if not requirements and standing_id is not None:
        standing = connection.execute(
            """SELECT unlock_json FROM campaign_standing_approvals
            WHERE id=? AND org_id=? AND project_id=?""",
            (standing_id, campaign["org_id"], campaign["project_id"]),
        ).fetchone()
        try:
            requirements = parse_unlock_json(None if standing is None else standing[0])
        except ValueError:
            raise Conflict(_STORED_INVALID) from None
    if [requirement_digest(item) for item in requirements] != [
        row["requirement_digest"] for row in rows
    ]:
        raise Conflict(
            "A forward re-approval must restate the first approval's unlock exactly "
            f"({len(rows)} requirement(s)); it cannot drop, change or add one."
        )
    return requirements


def record_evaluations(
    connection: sqlite3.Connection,
    *,
    org_id: str,
    project_id: str,
    campaign_id: str,
    approval_id: str,
    standing_approval_id: str | None,
    evaluations: list[dict[str, Any]],
    now: str,
) -> None:
    for item in evaluations:
        connection.execute(
            """INSERT INTO campaign_unlock_evaluations (id, org_id, project_id, campaign_id,
            approval_id, standing_approval_id, requirement_index, requirement_digest,
            named_campaign_id, basis_kind, gate_decision_id, outcome, result_digest,
            evaluated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "unlock_" + uuid.uuid4().hex,
                org_id,
                project_id,
                campaign_id,
                approval_id,
                standing_approval_id,
                item["requirement_index"],
                item["requirement_digest"],
                item["named_campaign_id"],
                item["basis_kind"],
                item["gate_decision_id"],
                item["outcome"],
                item["result_digest"],
                now,
            ),
        )


def unlock_receipt_section(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """What admitted this campaign: one entry per requirement per admission (a forward
    re-approval re-evaluates the first approval's), oldest admission first; an empty list
    when it was admitted unconditionally."""
    rows = connection.execute(
        """SELECT e.requirement_index, e.requirement_digest, e.named_campaign_id, e.basis_kind,
        e.gate_decision_id, e.outcome, e.result_digest, e.evaluated_at, e.standing_approval_id,
        g.decision_class AS gate_decision_class, g.delegation_id AS gate_delegation_id
        FROM campaign_unlock_evaluations e
        LEFT JOIN campaign_gate_decisions g ON g.id=e.gate_decision_id
        WHERE e.campaign_id=? ORDER BY e.rowid""",
        (campaign_id,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        # Keep the receipt's existing shape unless the go was a delegated one (ADR 0122).
        if item.pop("gate_delegation_id") is not None:
            item["gate_decision_class"] = item["gate_decision_class"]
            item["delegation_id"] = row["gate_delegation_id"]
        else:
            item.pop("gate_decision_class")
        items.append(item)
    return {"requirements": items}


class CampaignGateCommands:
    """Mixed into CampaignStore; relies on its _mutate, _actor, _campaign and _event."""

    def record_gate_decision(self, principal, campaign_id, key, digest, payload):
        try:
            body = RecordGateDecision.model_validate(payload)
        except ValidationError as error:
            first = error.errors()[0] if error.errors() else {}
            location = ".".join(str(part) for part in first.get("loc", ()))[:200]
            raise StoreError(f"Invalid gate decision: {location}") from None

        def command(connection, live):
            campaign = self._campaign(connection, live, campaign_id)
            # Agents, workers and external-scope credentials never write a gate decision.
            if live.get("kind") == "worker" or live["role"] != "human":
                raise Forbidden("A human must record a gate decision.")
            if self._external_scope(connection, live):
                raise Forbidden("A human must record a gate decision.")
            require_decision_actor(live, forbidden=Forbidden)
            if campaign["revision"] != body.expected_revision:
                raise Conflict("Campaign revision is no longer current.")
            if campaign["status"] not in GATEABLE_STATUSES:
                raise Conflict(
                    "A gate decision needs a finished (succeeded, stopped or failed) campaign."
                )
            if _exposure_unresolved(connection, campaign):
                raise Conflict("A gate decision needs the campaign's spend and attempts resolved.")
            provenance = decision_provenance(live)
            decision_id = "gatedec_" + uuid.uuid4().hex
            now = utc_now()
            connection.execute(
                """INSERT INTO campaign_gate_decisions (id, org_id, project_id, campaign_id,
                campaign_spec_digest, decision, note, decided_by_principal_id,
                decided_credential_id, decision_class, automation_label, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    campaign["org_id"],
                    campaign["project_id"],
                    campaign_id,
                    campaign["spec_digest"],
                    body.decision,
                    body.note,
                    live["id"],
                    live.get("credential_id"),
                    provenance["decision_class"],
                    provenance.get("automation_label"),
                    now,
                ),
            )
            self._event(
                connection,
                live,
                campaign["project_id"],
                None,
                "campaign.gate_decision_recorded",
                campaign["revision"],
                {
                    "campaign_id": campaign_id,
                    "gate_decision_id": decision_id,
                    "decision": body.decision,
                    "decision_class": provenance["decision_class"],
                },
            )
            return 201, {
                "id": decision_id,
                "campaign_id": campaign_id,
                "decision": body.decision,
                "note": body.note,
                "decided_by_principal_id": live["id"],
                "decision_class": provenance["decision_class"],
                "created_at": now,
                "scientific_validity": "not_assessed",
            }

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/gate-decision", digest, command
        )

    def list_gate_decisions(self, principal, campaign_id) -> dict:
        connection = _connect(self.database_path)
        try:
            actor = self._read_actor(connection, principal)
            self._campaign(connection, actor, campaign_id)
            rows = connection.execute(
                "SELECT * FROM campaign_gate_decisions WHERE campaign_id=? ORDER BY rowid",
                (campaign_id,),
            ).fetchall()
            decisions = [dict(row) for row in rows]
            if self._external_scope(connection, actor):
                # An agent sees the decision and note, never who decided or the credential,
                # matching tessera_campaign_standing_status.
                hidden = {"decided_by_principal_id", "decided_credential_id"}
                decisions = [
                    {k: v for k, v in item.items() if k not in hidden} for item in decisions
                ]
            return {
                "gate_decisions": decisions,
                "effective": decisions[-1] if decisions else None,
            }
        finally:
            connection.close()

    @staticmethod
    def check_unlock_references(
        connection: sqlite3.Connection, org_id: str, project_id: str, requirements
    ) -> None:
        """Creation-time sanity: every named campaign exists in this project. Whether it is
        finished and what it decided is evaluated at each admission, never here."""
        for requirement in requirements:
            found = connection.execute(
                "SELECT 1 FROM campaigns WHERE id=? AND project_id=? AND org_id=?",
                (requirement.campaign_id, project_id, org_id),
            ).fetchone()
            if found is None:
                raise NotFound("A named unlock campaign was not found in this project.")
