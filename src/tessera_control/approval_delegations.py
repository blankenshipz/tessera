"""Owner-signed approval delegations (ADR 0122): the record, its decisions, and its commands.

A person signs a delegation: written terms (budget ceiling, per-run cap, allowed providers,
optional approval count and gate rules) plus an expiry. Later code (the delegated-decision
commands) decides under it only when the written rule is met. This module owns the record:

* only a human creates or revokes one; agents and workers get 403, another tenant 404;
* the terms and every identifying column are immutable (database triggers); the only change
  after creation is ``active`` to ``revoked`` (with ``revision`` rising);
* every decision taken under a delegation, including an escalation, is an append-only row;
* events carry ids, the terms digest, the expiry and the caps and provider names, never the
  rule bodies.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import Field, ValidationError

from . import program_budget
from .approval_trust import decision_provenance, require_decision_actor
from .campaign_read_scope import own_decision_clause, reads_every_campaign
from .campaign_store import CampaignStore
from .delegation_rules import approval_count, committed_cents
from .delegation_terms import MAX_DELEGATION_DAYS, DelegationTerms, _Strict
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    canonical_digest,
    utc_now,
)

READ_SCOPE = "campaign:read"
STATUSES = ("active", "revoked")
STATES = ("active", "expired", "revoked")
LATEST_DECISIONS_LISTED = 20
MAX_PAGE = 200


# -- request models -------------------------------------------------------------------------


class CreateApprovalDelegation(_Strict):
    terms: DelegationTerms
    expires_at: Annotated[str, Field(min_length=1, max_length=64)]


class RevokeApprovalDelegation(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


# -- schema ---------------------------------------------------------------------------------


def create_delegation_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: shared by the migration and fresh databases so both converge."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS approval_delegations (
        id TEXT PRIMARY KEY CHECK(id GLOB 'deleg_*'),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_credential_id TEXT REFERENCES credentials(id),
        decision_class TEXT NOT NULL, automation_label TEXT,
        terms_json TEXT NOT NULL,
        terms_digest TEXT NOT NULL CHECK(length(terms_digest)=64
            AND terms_digest NOT GLOB '*[^0-9a-f]*'),
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        revoked_at TEXT,
        revoked_by_principal_id TEXT REFERENCES principals(id),
        created_at TEXT NOT NULL,
        CHECK((status='revoked') = (revoked_at IS NOT NULL)))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS approval_delegations_by_project "
        "ON approval_delegations(project_id, status)"
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS approval_delegations_terms_fixed
        BEFORE UPDATE ON approval_delegations
        WHEN NEW.id IS NOT OLD.id OR NEW.org_id IS NOT OLD.org_id
          OR NEW.project_id IS NOT OLD.project_id
          OR NEW.created_by_principal_id IS NOT OLD.created_by_principal_id
          OR NEW.created_credential_id IS NOT OLD.created_credential_id
          OR NEW.decision_class IS NOT OLD.decision_class
          OR NEW.automation_label IS NOT OLD.automation_label
          OR NEW.terms_json IS NOT OLD.terms_json OR NEW.terms_digest IS NOT OLD.terms_digest
          OR NEW.expires_at IS NOT OLD.expires_at OR NEW.created_at IS NOT OLD.created_at
          OR (NEW.status IS NOT OLD.status AND NOT (OLD.status='active' AND NEW.status='revoked'))
          OR (OLD.revoked_at IS NOT NULL AND (NEW.revoked_at IS NOT OLD.revoked_at
              OR NEW.revoked_by_principal_id IS NOT OLD.revoked_by_principal_id))
          OR NEW.revision < OLD.revision
        BEGIN SELECT RAISE(ABORT,'approval delegation terms are immutable'); END"""
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS approval_delegations_no_delete "
        "BEFORE DELETE ON approval_delegations BEGIN "
        "SELECT RAISE(ABORT,'approval delegations are kept'); END"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS delegated_decisions (
        id TEXT PRIMARY KEY CHECK(id GLOB 'ddec_*'),
        delegation_id TEXT NOT NULL REFERENCES approval_delegations(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        campaign_id TEXT NOT NULL REFERENCES campaigns(id),
        kind TEXT NOT NULL CHECK(kind IN ('approval','gate')),
        outcome TEXT NOT NULL CHECK(outcome IN ('approved','go','no_go','escalated')),
        reason TEXT CHECK(reason IS NULL OR length(reason) BETWEEN 1 AND 300),
        rule_name TEXT,
        evaluation_json TEXT NOT NULL,
        campaign_spec_digest TEXT NOT NULL,
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        actor_kind TEXT NOT NULL,
        approval_id TEXT, gate_decision_id TEXT,
        created_at TEXT NOT NULL)"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS delegated_decisions_by_delegation "
        "ON delegated_decisions(delegation_id, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS delegated_decisions_by_campaign "
        "ON delegated_decisions(campaign_id)"
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS delegated_decisions_no_{event.lower()}
            BEFORE {event} ON delegated_decisions
            BEGIN SELECT RAISE(ABORT,'delegated decisions are append-only'); END"""
        )


# -- helpers other code imports ---------------------------------------------------------------


def load_delegation(connection: sqlite3.Connection, delegation_id: str, org_id: str) -> sqlite3.Row:
    """The delegation row; NotFound when absent or another organization's."""
    row = connection.execute(
        "SELECT * FROM approval_delegations WHERE id=? AND org_id=?", (delegation_id, org_id)
    ).fetchone()
    if row is None:
        raise NotFound("Approval delegation was not found.")
    return row


def delegation_state(row: sqlite3.Row | dict, now: datetime) -> str:
    """``revoked`` beats ``expired``; both end the delegation's authority."""
    if row["status"] == "revoked":
        return "revoked"
    return "expired" if now >= _parse_utc(row["expires_at"]) else "active"


def parse_terms(row: sqlite3.Row | dict) -> DelegationTerms:
    return DelegationTerms.model_validate_json(row["terms_json"])


def record_decision(
    connection: sqlite3.Connection,
    *,
    delegation: sqlite3.Row | dict,
    kind: str,
    outcome: str,
    reason: str | None,
    rule_name: str | None,
    evaluation: dict[str, Any],
    campaign: sqlite3.Row | dict,
    actor: dict[str, Any],
    approval_id: str | None = None,
    gate_decision_id: str | None = None,
    now: str,
) -> str:
    """Append one delegated-decision row (caller's transaction); returns its id.

    ``evaluation`` is recorded verbatim as JSON: it must hold derived numbers and codes only.
    """
    ident = "ddec_" + uuid.uuid4().hex
    connection.execute(
        """INSERT INTO delegated_decisions (id, delegation_id, org_id, project_id, campaign_id,
        kind, outcome, reason, rule_name, evaluation_json, campaign_spec_digest,
        actor_principal_id, actor_kind, approval_id, gate_decision_id, created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            ident,
            delegation["id"],
            delegation["org_id"],
            campaign["project_id"],
            campaign["id"],
            kind,
            outcome,
            reason,
            rule_name,
            json.dumps(evaluation, sort_keys=True, separators=(",", ":")),
            campaign["spec_digest"],
            actor["id"],
            str(actor.get("kind") or actor.get("role") or "unknown"),
            approval_id,
            gate_decision_id,
            now,
        ),
    )
    return ident


# -- presentation -----------------------------------------------------------------------------


def _summary(row: sqlite3.Row | dict, now: datetime | None = None) -> dict[str, Any]:
    result = {k: v for k, v in dict(row).items() if k != "terms_json"}
    result["terms"] = json.loads(row["terms_json"])
    result["state"] = delegation_state(row, now or _parse_utc(utc_now()))
    return result


def _decision(row: sqlite3.Row) -> dict[str, Any]:
    return {
        **{k: v for k, v in dict(row).items() if k != "evaluation_json"},
        "evaluation": json.loads(row["evaluation_json"]),
    }


_SIGNER_KEYS = ("created_by_principal_id", "created_credential_id", "revoked_by_principal_id")


def _agent_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """An external-scope agent acts on a delegation's terms, never on who signed or revoked it."""
    return {k: v for k, v in summary.items() if k not in _SIGNER_KEYS}


def _agent_decision(decision: dict[str, Any], caller_id: str) -> dict[str, Any]:
    """Drop the acting principal unless it is the reading agent itself (a person may have acted)."""
    if decision.get("actor_principal_id") == caller_id:
        return decision
    return {k: v for k, v in decision.items() if k != "actor_principal_id"}


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid approval delegation: {location}: {str(first.get('msg', ''))[:200]}")


def _check_page(offset: int, limit: int) -> None:
    if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= MAX_PAGE:
        raise StoreError("Invalid page bounds.")


# -- store ------------------------------------------------------------------------------------


class ApprovalDelegationCommands(CampaignStore):
    """Delegation creation, revocation and reads over the campaign store's transactions."""

    def _require_human_signer(self, connection, actor) -> None:
        if actor.get("kind") == "worker" or actor["role"] != "human":
            raise Forbidden("A human must sign an approval delegation.")
        if self._external_scope(connection, actor):
            raise Forbidden("A human must sign an approval delegation.")
        require_decision_actor(actor, forbidden=Forbidden)

    def _reader(self, connection, principal) -> dict[str, Any]:
        actor = self._actor(connection, principal)
        self._require_action(connection, actor, READ_SCOPE)
        return actor

    @staticmethod
    def _visible(connection, actor, delegation_id: str) -> sqlite3.Row:
        row = load_delegation(connection, delegation_id, actor["org_id"])
        try:
            Store._project(connection, actor, row["project_id"])
        except NotFound:
            raise NotFound("Approval delegation was not found.") from None
        return row

    @staticmethod
    def _check_terms(connection, project_id: str, terms: DelegationTerms) -> None:
        from .campaign_providers import load_builtin_providers, provider_ids

        load_builtin_providers()
        known = set(provider_ids())
        unknown = sorted(p for p in terms.allowed_providers if p not in known)
        if unknown:
            raise StoreError(
                f"allowed_providers names an unregistered provider: {unknown[0][:100]}"
            )
        if terms.program_id is None:
            return
        config = program_budget.current()
        program = None
        if config is not None:
            program = next((p for p in config.programs if p.id == terms.program_id), None)
        if program is None:
            raise StoreError("program_id is not a configured program.")
        if program.project_ids is not None and project_id not in program.project_ids:
            raise StoreError("program_id does not cover this project.")

    # create / revoke ---------------------------------------------------------------------------

    def create_approval_delegation(self, principal, project_id, key, digest, payload):
        try:
            body = CreateApprovalDelegation.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_human_signer(connection, live)
            terms = body.terms
            self._check_terms(connection, project_id, terms)
            created = _parse_utc(utc_now())
            try:
                expires = _parse_utc(body.expires_at)
            except ValueError:
                raise StoreError("expires_at must be an ISO-8601 time with an offset.") from None
            if expires <= created or expires > created + timedelta(days=MAX_DELEGATION_DAYS):
                raise StoreError(f"expires_at must be within {MAX_DELEGATION_DAYS} days from now.")
            terms_document = terms.model_dump(mode="json")
            provenance = decision_provenance(live)
            row = {
                "id": "deleg_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project_id,
                "created_by_principal_id": live["id"],
                "created_credential_id": live["credential_id"],
                "decision_class": provenance["decision_class"],
                "automation_label": provenance.get("automation_label"),
                "terms_json": json.dumps(terms_document, sort_keys=True, separators=(",", ":")),
                "terms_digest": canonical_digest(terms_document),
                "expires_at": _utc_string(expires),
                "status": "active",
                "revision": 1,
                "revoked_at": None,
                "revoked_by_principal_id": None,
                "created_at": _utc_string(created),
            }
            names = ", ".join(row)
            marks = ", ".join(f":{k}" for k in row)
            connection.execute(f"INSERT INTO approval_delegations ({names}) VALUES ({marks})", row)
            self._event(
                connection,
                live,
                project_id,
                None,
                "campaign.delegation_created",
                1,
                {
                    "delegation_id": row["id"],
                    "terms_digest": row["terms_digest"],
                    "expires_at": row["expires_at"],
                    "program_id": terms.program_id,
                    "budget_ceiling_cents": terms.budget_ceiling_cents,
                    "per_run_cap_cents": terms.per_run_cap_cents,
                    "allowed_providers": list(terms.allowed_providers),
                    **provenance,
                },
            )
            return 201, _summary(row, created)

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/approval-delegations", digest, command
        )

    def revoke_approval_delegation(self, principal, delegation_id, key, digest, payload):
        try:
            body = RevokeApprovalDelegation.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._require_human_signer(connection, live)
            row = self._visible(connection, live, delegation_id)
            if row["revision"] != body.expected_revision:
                raise Conflict("Approval delegation revision is no longer current.")
            if row["status"] != "active":
                raise Conflict(f"Approval delegation is already {row['status']}.")
            stamp, revision = utc_now(), row["revision"] + 1
            connection.execute(
                """UPDATE approval_delegations SET status='revoked', revision=?, revoked_at=?,
                revoked_by_principal_id=? WHERE id=? AND status='active'""",
                (revision, stamp, live["id"], row["id"]),
            )
            self._event(
                connection,
                live,
                row["project_id"],
                None,
                "campaign.delegation_revoked",
                revision,
                {
                    "delegation_id": row["id"],
                    "terms_digest": row["terms_digest"],
                    **decision_provenance(live),
                },
            )
            updated = connection.execute(
                "SELECT * FROM approval_delegations WHERE id=?", (row["id"],)
            ).fetchone()
            return 200, _summary(updated)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/approval-delegations/{delegation_id}/revoke",
            digest,
            command,
        )

    # reads -------------------------------------------------------------------------------------

    def list_approval_delegations(
        self, principal, project_id, status=None, offset=0, limit=50
    ) -> dict[str, Any]:
        _check_page(offset, limit)
        if status is not None and status not in STATUSES:
            raise StoreError("Invalid status filter.")
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM approval_delegations WHERE project_id=? AND org_id=?
                AND (? IS NULL OR status=?) ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, actor["org_id"], status, status, limit, offset),
            ).fetchall()
            now = _parse_utc(utc_now())
            summaries = [_summary(r, now) for r in rows]
            if self._external_scope(connection, actor):
                summaries = [_agent_summary(x) for x in summaries]
            return {"approval_delegations": summaries}
        finally:
            connection.close()

    def get_approval_delegation(self, principal, delegation_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal)
            row = self._visible(connection, actor, delegation_id)
            # Audit O-05: an agent sees decisions on its own campaigns only, and not the usage
            # totals, which sum every campaign the delegation approved.
            clause, args = own_decision_clause(actor, "d.campaign_id", "d.actor_principal_id")
            latest = connection.execute(
                f"""SELECT d.* FROM delegated_decisions d WHERE d.delegation_id=?{clause}
                ORDER BY d.created_at DESC, d.rowid DESC LIMIT ?""",
                (row["id"], *args, LATEST_DECISIONS_LISTED),
            ).fetchall()
            usage: dict[str, Any] = (
                {
                    "committed_cents": committed_cents(connection, row["id"]),
                    "approvals": approval_count(connection, row["id"]),
                }
                if reads_every_campaign(actor)
                else {"withheld": "usage sums campaigns other principals submitted"}
            )
            summary = _summary(row)
            decisions = [_decision(r) for r in latest]
            if self._external_scope(connection, actor):
                summary = _agent_summary(summary)
                decisions = [_agent_decision(d, actor["id"]) for d in decisions]
            return {
                **summary,
                "usage": usage,
                "decisions": decisions,
            }
        finally:
            connection.close()

    def list_delegated_decisions(
        self, principal, delegation_id, offset=0, limit=50
    ) -> dict[str, Any]:
        _check_page(offset, limit)
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal)
            row = self._visible(connection, actor, delegation_id)
            clause, args = own_decision_clause(
                actor, "d.campaign_id", "d.actor_principal_id"
            )  # audit O-05
            rows = connection.execute(
                f"""SELECT d.* FROM delegated_decisions d WHERE d.delegation_id=?{clause}
                ORDER BY d.created_at, d.rowid LIMIT ? OFFSET ?""",
                (row["id"], *args, limit, offset),
            ).fetchall()
            decisions = [_decision(r) for r in rows]
            if self._external_scope(connection, actor):
                decisions = [_agent_decision(d, actor["id"]) for d in decisions]
            return {"decisions": decisions}
        finally:
            connection.close()


__all__ = [
    "ApprovalDelegationCommands",
    "CreateApprovalDelegation",
    "RevokeApprovalDelegation",
    "approval_count",
    "committed_cents",
    "create_delegation_schema",
    "delegation_state",
    "load_delegation",
    "parse_terms",
    "record_decision",
]
