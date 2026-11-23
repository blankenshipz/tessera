"""Decisions made under an owner-signed delegation (ADR 0122): approve a campaign, record a gate
go or no-go, only when the delegation's written rule is met.

Who may ask: a human, or a non-human credential enrolled with the separate scope
``campaign:decide_delegated`` (it is not part of ``--allow-campaigns``). The caller never supplies a
fact the platform decides: the rules (``delegation_rules``) are evaluated from recorded data inside
the transaction, under the write lock, so a ceiling or a cap cannot be raced. A decision that meets
the rule is recorded on a receipt that cites the delegation; anything else fails closed (a 409
naming stable reason codes), is recorded as an ``escalated`` decision for the owner to see, and
changes nothing. A delegated approval is only ever a campaign's first approval and carries no
unlock requirement and no review pre-approval of its own, so it cannot supply what a person must;
a delegation's optional review rule lets the runner record one at the review stop (ADR 0150).
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError

from . import program_budget, provider_trust
from .approval_delegations import (
    ApprovalDelegationCommands,
    load_delegation,
    record_decision,
)
from .approval_trust import (
    CLASS_OWNER_DELEGATED,
    OWNER_DELEGATION_KEY,
    require_decision_actor,
)
from .campaign_models import parse_spec_json, spec_digest
from .delegation_receipts import delegation_receipt_section  # noqa: F401 - re-exported
from .delegation_rules import evaluate_approval, evaluate_gate
from .delegation_terms import Identifier, Slug, _Strict
from .store import (
    Conflict,
    Forbidden,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    utc_now,
)

DECIDE_SCOPE = "campaign:decide_delegated"
Digest = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Revision = Annotated[int, Field(strict=True, ge=1)]
MAX_LISTED = 200


class DelegatedApprove(_Strict):
    delegation_id: Identifier
    expected_revision: Revision
    expected_spec_digest: Digest


class DelegatedGateDecision(_Strict):
    delegation_id: Identifier
    rule: Slug
    expected_revision: Revision


class DelegationRefused(Conflict):
    """The delegation's rule is not met: nothing was decided, the owner is asked."""

    def __init__(
        self, kind: str, reasons: list[str], delegation_id: str, campaign_id: str, live: dict
    ) -> None:
        self.kind, self.reasons = kind, sorted(set(reasons))
        self.delegation_id, self.campaign_id, self.live = delegation_id, campaign_id, live
        super().__init__(
            "outside_delegation: " + ", ".join(self.reasons) + "; the owner has been asked "
            "(nothing was approved or recorded)"
        )


def _acting(live: dict[str, Any], delegation: sqlite3.Row) -> dict[str, Any]:
    """The caller, marked as acting under this delegation so every decision event it writes is
    stamped ``owner_delegated`` with the delegation id (the marker lives only in this process)."""
    return {**live, OWNER_DELEGATION_KEY: {"delegation_id": delegation["id"]}}


def _creator(connection: sqlite3.Connection, delegation: sqlite3.Row) -> dict[str, Any] | None:
    try:
        return Store._live_principal(
            connection,
            {"id": delegation["created_by_principal_id"], "org_id": delegation["org_id"]},
        )
    except Forbidden:
        return None


class DelegatedDecisionStore(ApprovalDelegationCommands):
    def _require_decider(self, connection, live) -> None:
        """A human acts directly; any other credential needs the explicit enrolled scope."""
        if live.get("kind") == "worker":
            raise Forbidden("Workers cannot make delegated decisions.")
        if live["role"] == "human" and not self._external_scope(connection, live):
            require_decision_actor(live, forbidden=Forbidden)
            return
        self._scope(connection, live, DECIDE_SCOPE)

    # -- approval --------------------------------------------------------------------------

    def delegated_approve(self, principal, campaign_id, key, digest, payload):
        try:
            body = DelegatedApprove.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._require_decider(connection, live)
            campaign = self._campaign(connection, live, campaign_id)
            delegation = load_delegation(connection, body.delegation_id, campaign["org_id"])
            if (
                campaign["revision"] != body.expected_revision
                or campaign["spec_digest"] != body.expected_spec_digest
            ):
                raise Conflict("Campaign revision or spec digest is no longer current.")
            now_dt = _parse_utc(utc_now())
            now = _utc_string(now_dt)

            def refuse(reasons: list[str]):
                return DelegationRefused("approval", reasons, delegation["id"], campaign_id, live)

            try:
                spec = parse_spec_json(campaign["spec_json"], providers=True)
                if spec_digest(spec) != campaign["spec_digest"]:
                    raise ValueError("digest")
            except Exception:  # noqa: BLE001 - an unparseable stored spec is a refusal
                raise refuse(["spec_invalid"]) from None
            creator = _creator(connection, delegation)
            evaluation = evaluate_approval(
                connection, delegation, campaign, spec, now=now_dt, creator=creator
            )
            reasons = list(evaluation.reasons)
            if evaluation.ok and creator is not None:
                try:
                    self._check_references(
                        connection, creator, campaign["project_id"], spec, campaign_id=campaign_id
                    )
                except sqlite3.OperationalError:
                    raise  # transient storage trouble is not a refusal
                except Exception:  # noqa: BLE001 - trust, data-class and reference refusals
                    reasons.append("spec_references_invalid")
                admission = program_budget.check_admission(
                    connection, campaign["project_id"], campaign["budget_cap_cents"]
                )
                if admission is not None:
                    reasons.append(admission.split(":", 1)[0])
            if reasons:
                raise refuse(reasons)
            acting = _acting(live, delegation)
            approval = {
                "id": "approval_" + uuid.uuid4().hex,
                "campaign_id": campaign_id,
                "spec_digest": campaign["spec_digest"],
                # The runner acts as the approving human and checks that human is still live:
                # the person who signed the delegation stands behind this approval.
                "approving_principal_id": delegation["created_by_principal_id"],
                "decision_class": CLASS_OWNER_DELEGATED,
                "automation_label": None,
                "approved_at": now,
                "expires_at": _utc_string(now_dt + timedelta(seconds=spec.deadline_seconds)),
                "status": "active",
                "ended_at": None,
                "trust_policy_digest": provider_trust.current_policy(connection).digest,
                "delegation_id": delegation["id"],
            }
            connection.execute(
                """INSERT INTO campaign_approvals (id, campaign_id, spec_digest,
                approving_principal_id, decision_class, automation_label, approved_at,
                expires_at, status, ended_at, trust_policy_digest, delegation_id) VALUES (:id,
                :campaign_id,:spec_digest,:approving_principal_id,:decision_class,
                :automation_label,:approved_at,:expires_at,:status,:ended_at,
                :trust_policy_digest,:delegation_id)""",
                approval,
            )
            revision = campaign["revision"] + 1
            connection.execute(
                "UPDATE campaigns SET status='running', revision=?, updated_at=? WHERE id=?",
                (revision, now, campaign_id),
            )
            self._event(
                connection,
                acting,
                campaign["project_id"],
                None,
                "campaign.approved",
                revision,
                {
                    "campaign_id": campaign_id,
                    "approval_id": approval["id"],
                    "spec_digest": campaign["spec_digest"],
                    "expires_at": approval["expires_at"],
                    "superseded_approval_id": None,
                    "resulting_status": "running",
                    "preapprovals": [],
                },
            )
            decision_id = record_decision(
                connection,
                delegation=delegation,
                kind="approval",
                outcome="approved",
                reason=None,
                rule_name=None,
                evaluation=dict(evaluation.details),
                campaign=campaign,
                actor=live,
                approval_id=approval["id"],
                now=now,
            )
            self._event(
                connection,
                acting,
                campaign["project_id"],
                None,
                "campaign.delegated_approval",
                revision,
                {
                    "campaign_id": campaign_id,
                    "delegation_id": delegation["id"],
                    "delegated_decision_id": decision_id,
                    "approval_id": approval["id"],
                },
            )
            from .campaign_store import _summary

            return 200, {
                **_summary(campaign),
                "status": "running",
                "revision": revision,
                "updated_at": now,
                "approval": approval,
                "delegated_decision_id": decision_id,
            }

        return self._run(
            principal, key, f"/campaigns/{campaign_id}/delegated-approval", digest, command
        )

    # -- gate decision ---------------------------------------------------------------------

    def delegated_gate_decision(self, principal, campaign_id, key, digest, payload):
        try:
            body = DelegatedGateDecision.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._require_decider(connection, live)
            campaign = self._campaign(connection, live, campaign_id)
            delegation = load_delegation(connection, body.delegation_id, campaign["org_id"])
            if campaign["revision"] != body.expected_revision:
                raise Conflict("Campaign revision is no longer current.")
            now_dt = _parse_utc(utc_now())
            now = _utc_string(now_dt)
            evaluation = evaluate_gate(connection, delegation, campaign, body.rule, now=now_dt)
            if not evaluation.ok or evaluation.decision is None:
                raise DelegationRefused(
                    "gate", list(evaluation.reasons) or ["no_decision"], delegation["id"],
                    campaign_id, live,
                )  # fmt: skip
            creator = _creator(connection, delegation)
            if creator is None:
                raise DelegationRefused(
                    "gate", ["creator_not_live"], delegation["id"], campaign_id, live
                )
            acting = _acting(live, delegation)
            decision_id = "gatedec_" + uuid.uuid4().hex
            outcome = (evaluation.basis or {}).get("outcome")
            note = f"delegated rule {body.rule}: pre-registered bound outcome {outcome}"
            connection.execute(
                """INSERT INTO campaign_gate_decisions (id, org_id, project_id, campaign_id,
                campaign_spec_digest, decision, note, decided_by_principal_id,
                decided_credential_id, decision_class, automation_label, created_at,
                delegation_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    decision_id,
                    campaign["org_id"],
                    campaign["project_id"],
                    campaign_id,
                    campaign["spec_digest"],
                    evaluation.decision,
                    note,
                    delegation["created_by_principal_id"],
                    delegation["created_credential_id"],
                    CLASS_OWNER_DELEGATED,
                    None,
                    now,
                    delegation["id"],
                ),
            )
            ddec = record_decision(
                connection,
                delegation=delegation,
                kind="gate",
                outcome=evaluation.decision,
                reason=None,
                rule_name=body.rule,
                evaluation={"reasons": list(evaluation.reasons), "basis": evaluation.basis},
                campaign=campaign,
                actor=live,
                gate_decision_id=decision_id,
                now=now,
            )
            self._event(
                connection,
                acting,
                campaign["project_id"],
                None,
                "campaign.delegated_gate_decision",
                campaign["revision"],
                {
                    "campaign_id": campaign_id,
                    "gate_decision_id": decision_id,
                    "delegation_id": delegation["id"],
                    "delegated_decision_id": ddec,
                    "decision": evaluation.decision,
                    "rule": body.rule,
                },
            )
            return 201, {
                "gate_decision_id": decision_id,
                "decision": evaluation.decision,
                "rule": body.rule,
                "delegation_id": delegation["id"],
                "delegated_decision_id": ddec,
                "decision_class": CLASS_OWNER_DELEGATED,
            }

        return self._run(
            principal, key, f"/campaigns/{campaign_id}/delegated-gate-decision", digest, command
        )

    # -- shared ----------------------------------------------------------------------------

    def _run(self, principal, key, path, digest, command):
        """Run a command; a refusal rolled the command back, so record it as an escalation in its
        own transaction before the error reaches the caller."""
        try:
            return self._mutate(principal, key, "POST", path, digest, command)
        except DelegationRefused as refusal:
            self._record_escalation(refusal)
            raise

    def _record_escalation(self, refusal: DelegationRefused) -> None:
        live = refusal.live
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            delegation = connection.execute(
                "SELECT * FROM approval_delegations WHERE id=?", (refusal.delegation_id,)
            ).fetchone()
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE id=?", (refusal.campaign_id,)
            ).fetchone()
            if delegation is None or campaign is None or live is None:
                connection.execute("ROLLBACK")
                return
            reason = ",".join(refusal.reasons)[:300]
            last = connection.execute(
                """SELECT outcome, reason FROM delegated_decisions WHERE delegation_id=? AND
                campaign_id=? AND kind=? ORDER BY rowid DESC LIMIT 1""",
                (delegation["id"], campaign["id"], refusal.kind),
            ).fetchone()
            if last is not None and last["outcome"] == "escalated" and last["reason"] == reason:
                connection.execute("ROLLBACK")  # the owner was already asked for exactly this
                return
            now = _utc_string(_parse_utc(utc_now()))
            ddec = record_decision(
                connection,
                delegation=delegation,
                kind=refusal.kind,
                outcome="escalated",
                reason=reason,
                rule_name=None,
                evaluation={"reasons": refusal.reasons},
                campaign=campaign,
                actor=live,
                now=now,
            )
            Store._event(
                connection,
                live,
                campaign["project_id"],
                None,
                "campaign.delegation_escalated",
                campaign["revision"],
                {
                    "campaign_id": campaign["id"],
                    "delegation_id": delegation["id"],
                    "delegated_decision_id": ddec,
                    "kind": refusal.kind,
                    "reasons": refusal.reasons,
                },
            )
            connection.execute("COMMIT")
        except sqlite3.Error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        finally:
            connection.close()

    def list_delegation_escalations(self, principal, project_id, offset=0, limit=50):
        """What the owner has been asked to decide: escalated decisions of active delegations."""
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= MAX_LISTED:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT d.* FROM delegated_decisions d WHERE d.project_id=? AND d.org_id=?
                AND d.outcome='escalated' ORDER BY d.created_at DESC, d.id LIMIT ? OFFSET ?""",
                (project_id, actor["org_id"], limit, offset),
            ).fetchall()
            return {
                "escalations": [
                    {
                        "id": row["id"],
                        "delegation_id": row["delegation_id"],
                        "campaign_id": row["campaign_id"],
                        "kind": row["kind"],
                        "reasons": (row["reason"] or "").split(","),
                        "created_at": row["created_at"],
                    }
                    for row in rows
                ]
            }
        finally:
            connection.close()


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid delegated decision request: {location}")
