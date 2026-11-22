"""Approved campaign submissions (ADR 0093).

A human records an *approved submission*: the exact spec digest of a campaign to submit
(``kind=submit``) or a planned re-run of a finished campaign (``kind=rerun``), with a per-run
cap and an expiry. An agent (or a session) later *consumes* it. Consuming decides nothing
new: it creates the campaign and an ordinary approval row whose approving principal is the
human creator (decision class ``approved_submission``), exactly as a standing start does.

Invariants (each enforced here and tested):

* only a human creates an approval; agents and workers get 403;
* the consuming actor is recorded on the decision, never as the approving principal;
* nothing an agent sends can widen a cap, change the spec or digest, or create a standing
  approval or pre-approval; the stored terms are immutable (database triggers);
* the feature is off unless the operator enables it, and a kill switch marker file whose
  state is ambiguous counts as engaged;
* at most one campaign per approval (status guard under ``BEGIN IMMEDIATE``, UNIQUE columns
  and a trigger that never lets ``campaign_id`` change once set);
* every check runs before anything is created; a refusal creates nothing and is recorded
  (a decision row and an event) in its own transaction, so it is never replayed from the
  idempotency record after the condition clears;
* decisions are append-only.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import sqlite3
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from . import program_budget, provider_trust
from .approval_trust import decision_provenance, require_decision_actor
from .campaign_io import refuse_submitted_lineage
from .campaign_models import (
    AnyCampaignSpec,
    Cents,
    Identifier,
    Sha256Digest,
    _Strict,
    normalize,
    parse_spec,
    parse_spec_json,
    spec_digest,
    stage_provider,
)
from .campaign_rerun import (
    FINISHED,
    RERUN_EVENT,
    RerunCampaign,
    RerunRefused,
    lineage_remaining,
    load_source,
    plan_rerun,
)
from .campaign_salad_adapter import expand_with_registered
from .campaign_standing import MAX_WINDOW_DAYS, _headroom, insert_approved_campaign
from .campaign_store import CampaignStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    utc_now,
)

log = logging.getLogger("tessera.campaign_submissions")

CLASS_APPROVED_SUBMISSION = "approved_submission"
SUBMIT_SCOPE = "campaign:submit_cleared"
CANCEL_SCOPE = "campaign:cancel"
READ_SCOPE = "campaign:read"
STATUSES = ("pending", "submitted", "cancelled", "expired", "refused_permanent")
MAX_DECISIONS_LISTED = 200

# Refusals that end the approval: the condition can never clear.
PERMANENT_REASONS = frozenset(
    {
        "expired",
        "creator_not_live",
        "creator_not_human",
        "creator_credential_changed",
        "creator_not_project_member",
        "spec_invalid",
        "spec_digest_mismatch",
        "per_run_cap_exceeds_spec",
        "rerun_source_not_finished",
    }
)


# -- operator configuration -----------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class AgentSubmissionConfig:
    """Operator switch for consuming approved submissions; off unless set (fail closed)."""

    enabled: bool = False
    kill_switch_path: str | None = None


def kill_switch_engaged(config: AgentSubmissionConfig) -> bool:
    """Engaged if the marker exists or its state is ambiguous (no path, unreadable parent)."""
    if config.kill_switch_path is None:
        return True
    path = Path(config.kill_switch_path)
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    try:
        return not path.parent.is_dir()
    except OSError:
        return True


# -- request models -------------------------------------------------------------------------


class CreateSubmissionApproval(_Strict):
    kind: Literal["submit", "rerun"]
    spec: AnyCampaignSpec | None = None
    expected_spec_digest: Sha256Digest | None = None
    source_campaign_id: Identifier | None = None
    mode: Literal["failed_stages", "identical"] | None = None
    stage_keys: Annotated[list[Identifier], Field(min_length=1, max_length=64)] | None = None
    per_run_budget_cap_cents: Cents
    not_after: Annotated[str, Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def _shape(self) -> CreateSubmissionApproval:
        if self.kind == "submit":
            if self.spec is None or self.expected_spec_digest is None:
                raise ValueError("kind=submit needs spec and expected_spec_digest")
            if self.source_campaign_id or self.mode or self.stage_keys:
                raise ValueError("kind=submit takes no re-run fields")
        else:
            if self.spec is not None or self.source_campaign_id is None:
                raise ValueError("kind=rerun needs source_campaign_id and no spec")
        return self


class SubmitApprovedSubmission(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


class CancelSubmissionApproval(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Annotated[str, Field(min_length=1, max_length=1000)]


class SubmissionRefused(Conflict):
    """A recorded refusal with a stable ``reason`` code (409)."""

    def __init__(
        self,
        reason: str,
        approval_id: str,
        *,
        revision: int,
        details: str | None = None,
        live: dict[str, Any] | None = None,
        record: bool = True,
    ) -> None:
        super().__init__(f"Submission refused: {reason}")
        self.reason = reason[:300]
        self.approval_id = approval_id
        self.revision = revision
        self.details = None if details is None else details[:300]
        self.permanent = reason in PERMANENT_REASONS
        self.live = live
        self.record = record

    def body(self) -> dict[str, Any]:
        return {
            "detail": str(self),
            "reason": self.reason,
            "approval_id": self.approval_id,
            "permanent": self.permanent,
            **({"details": self.details} if self.details else {}),
        }


# -- schema ---------------------------------------------------------------------------------


def create_submission_schema(connection: sqlite3.Connection) -> None:
    """Idempotent: shared by the migration and fresh databases so both converge."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS campaign_submission_approvals (
        id TEXT PRIMARY KEY CHECK(id GLOB 'submission_*'),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        kind TEXT NOT NULL CHECK(kind IN ('submit','rerun')),
        spec_json TEXT NOT NULL,
        spec_digest TEXT NOT NULL CHECK(length(spec_digest)=64
            AND spec_digest NOT GLOB '*[^0-9a-f]*'),
        source_campaign_id TEXT REFERENCES campaigns(id),
        rerun_mode TEXT CHECK(rerun_mode IS NULL OR rerun_mode IN ('failed_stages','identical')),
        lineage_json TEXT,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_credential_id TEXT REFERENCES credentials(id),
        decision_class TEXT NOT NULL, automation_label TEXT,
        trust_policy_digest TEXT NOT NULL,
        per_run_budget_cap_cents INTEGER NOT NULL
            CHECK(typeof(per_run_budget_cap_cents)='integer'
            AND per_run_budget_cap_cents BETWEEN 0 AND 5000),
        not_after TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('pending','submitted','cancelled','expired','refused_permanent')),
        campaign_id TEXT UNIQUE REFERENCES campaigns(id),
        approval_id TEXT UNIQUE REFERENCES campaign_approvals(id),
        last_refusal_reason TEXT CHECK(last_refusal_reason IS NULL
            OR length(last_refusal_reason) BETWEEN 1 AND 300),
        revision INTEGER NOT NULL CHECK(revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        CHECK((status='submitted') = (campaign_id IS NOT NULL AND approval_id IS NOT NULL)),
        CHECK((kind='rerun') = (source_campaign_id IS NOT NULL)))"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS campaign_submission_approvals_by_project "
        "ON campaign_submission_approvals(project_id, status)"
    )
    # The human's terms never change after creation, a submitted approval never moves to
    # another campaign, and a final status is final.
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS campaign_submission_approvals_terms_fixed
        BEFORE UPDATE ON campaign_submission_approvals
        WHEN NEW.id IS NOT OLD.id OR NEW.org_id IS NOT OLD.org_id
          OR NEW.project_id IS NOT OLD.project_id OR NEW.kind IS NOT OLD.kind
          OR NEW.spec_json IS NOT OLD.spec_json OR NEW.spec_digest IS NOT OLD.spec_digest
          OR NEW.source_campaign_id IS NOT OLD.source_campaign_id
          OR NEW.rerun_mode IS NOT OLD.rerun_mode OR NEW.lineage_json IS NOT OLD.lineage_json
          OR NEW.created_by_principal_id IS NOT OLD.created_by_principal_id
          OR NEW.created_credential_id IS NOT OLD.created_credential_id
          OR NEW.decision_class IS NOT OLD.decision_class
          OR NEW.automation_label IS NOT OLD.automation_label
          OR NEW.trust_policy_digest IS NOT OLD.trust_policy_digest
          OR NEW.per_run_budget_cap_cents IS NOT OLD.per_run_budget_cap_cents
          OR NEW.not_after IS NOT OLD.not_after OR NEW.created_at IS NOT OLD.created_at
          OR (OLD.campaign_id IS NOT NULL AND NEW.campaign_id IS NOT OLD.campaign_id)
          OR (OLD.approval_id IS NOT NULL AND NEW.approval_id IS NOT OLD.approval_id)
          OR (OLD.status <> 'pending' AND NEW.status IS NOT OLD.status)
          OR NEW.revision < OLD.revision
        BEGIN SELECT RAISE(ABORT,'campaign submission approval terms are immutable'); END"""
    )
    connection.execute(
        "CREATE TRIGGER IF NOT EXISTS campaign_submission_approvals_no_delete "
        "BEFORE DELETE ON campaign_submission_approvals BEGIN "
        "SELECT RAISE(ABORT,'campaign submission approvals are kept'); END"
    )
    connection.execute(
        """CREATE TABLE IF NOT EXISTS campaign_submission_decisions (
        id TEXT PRIMARY KEY CHECK(id GLOB 'subdecision_*'),
        approval_id TEXT NOT NULL REFERENCES campaign_submission_approvals(id),
        decision TEXT NOT NULL CHECK(decision IN ('submitted','refused','cancelled','expired')),
        reason TEXT CHECK(reason IS NULL OR length(reason) BETWEEN 1 AND 1000),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        decision_class TEXT NOT NULL,
        details_json TEXT NOT NULL,
        created_at TEXT NOT NULL)"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS campaign_submission_decisions_by_approval "
        "ON campaign_submission_decisions(approval_id, created_at)"
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""CREATE TRIGGER IF NOT EXISTS campaign_submission_decisions_no_{event.lower()}
            BEFORE {event} ON campaign_submission_decisions
            BEGIN SELECT RAISE(ABORT,'campaign submission decisions are append-only'); END"""
        )


# -- helpers --------------------------------------------------------------------------------


def _summary(row: sqlite3.Row | dict) -> dict[str, Any]:
    result = {k: v for k, v in dict(row).items() if k not in {"spec_json", "lineage_json"}}
    result["lineage"] = json.loads(row["lineage_json"]) if row["lineage_json"] else None
    return result


def _decisions(connection: sqlite3.Connection, approval_id: str) -> list[dict[str, Any]]:
    rows = connection.execute(
        """SELECT * FROM campaign_submission_decisions WHERE approval_id=?
        ORDER BY created_at, rowid LIMIT ?""",
        (approval_id, MAX_DECISIONS_LISTED),
    ).fetchall()
    return [
        {
            **{k: v for k, v in dict(r).items() if k != "details_json"},
            "details": json.loads(r["details_json"]),
        }
        for r in rows
    ]


def _decide(
    connection: sqlite3.Connection,
    live: dict[str, Any],
    row: sqlite3.Row | dict,
    decision: str,
    reason: str | None,
    details: dict[str, Any],
    revision: int,
) -> str:
    """Append one decision row and its event (same transaction as the state change)."""
    ident = "subdecision_" + uuid.uuid4().hex
    provenance = decision_provenance(live)
    connection.execute(
        """INSERT INTO campaign_submission_decisions (id, approval_id, decision, reason,
        actor_principal_id, decision_class, details_json, created_at)
        VALUES (?,?,?,?,?,?,?,?)""",
        (
            ident,
            row["id"],
            decision,
            reason,
            live["id"],
            provenance["decision_class"],
            json.dumps(details, sort_keys=True, separators=(",", ":")),
            utc_now(),
        ),
    )
    Store._event(
        connection,
        live,
        row["project_id"],
        None,
        f"campaign.submission_{decision}",
        revision,
        {
            "submission_approval_id": row["id"],
            "decision_id": ident,
            "reason": reason,
            **details,
            **provenance,
        },
    )
    return ident


def submission_receipt_section(connection: sqlite3.Connection, campaign_id: str) -> dict[str, Any]:
    """The approved submission a campaign was created from; empty otherwise, so existing
    receipts keep their shape and digest."""
    row = connection.execute(
        "SELECT * FROM campaign_submission_approvals WHERE campaign_id=?", (campaign_id,)
    ).fetchone()
    if row is None:
        return {}
    return {
        "submission": {
            "submission_approval_id": row["id"],
            "kind": row["kind"],
            "spec_digest": row["spec_digest"],
            "per_run_budget_cap_cents": row["per_run_budget_cap_cents"],
            "approved_by_principal_id": row["created_by_principal_id"],
            "approval_decision_class": row["decision_class"],
            "source_campaign_id": row["source_campaign_id"],
            "decisions": [
                {
                    "decision": d["decision"],
                    "reason": d["reason"],
                    "actor_principal_id": d["actor_principal_id"],
                    "decision_class": d["decision_class"],
                    "created_at": d["created_at"],
                }
                for d in _decisions(connection, row["id"])
            ],
        }
    }


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid submission approval: {location}: {str(first.get('msg', ''))[:200]}")


# -- store ----------------------------------------------------------------------------------


class CampaignSubmissionStore(CampaignStore):
    """Approved-submission commands and reads over the campaign store's transactions."""

    # authorization ---------------------------------------------------------------------------

    def _require_human_decider(self, connection, actor) -> None:
        if actor.get("kind") == "worker" or actor["role"] != "human":
            raise Forbidden("A human must approve a campaign submission.")
        if self._external_scope(connection, actor):
            raise Forbidden("A human must approve a campaign submission.")
        require_decision_actor(actor, forbidden=Forbidden)

    def _require_scoped(self, connection, actor, action: str) -> None:
        """Humans act directly; any other credential needs the explicit enrolled scope."""
        if actor.get("kind") == "worker":
            raise Forbidden("Workers cannot use campaign submissions.")
        if actor["role"] == "human" and not self._external_scope(connection, actor):
            return
        self._scope(connection, actor, action)

    @staticmethod
    def _row(connection, actor, approval_id: str) -> sqlite3.Row:
        row = connection.execute(
            """SELECT a.* FROM campaign_submission_approvals a
            JOIN projects p ON p.id=a.project_id WHERE a.id=? AND p.org_id=?""",
            (approval_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Submission approval was not found.")
        try:
            Store._project(connection, actor, row["project_id"])
        except NotFound:
            raise NotFound("Submission approval was not found.") from None
        return row

    # create ----------------------------------------------------------------------------------

    def create_submission_approval(self, principal, project_id, key, digest, payload):
        try:
            body = CreateSubmissionApproval.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        raw_spec = payload.get("spec") if isinstance(payload, dict) else None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_human_decider(connection, live)
            lineage = None
            source_id = None
            if body.kind == "submit":
                refuse_submitted_lineage(raw_spec)
                spec = expand_with_registered(
                    body.spec,
                    self.adapter_entries(connection, live, project_id),
                    tenant=(live["org_id"], project_id),
                )
                if any(
                    getattr(source, "kind", None) == "receipt_output"
                    for stage in spec.stages
                    for source in getattr(stage, "inputs", [])
                ):
                    raise Conflict(
                        "An approved submission cannot use receipt_output inputs: pin the value "
                        "with an artifact input (campaign_id, stage, output, sha256) instead."
                    )
            else:
                source = self._campaign(connection, live, body.source_campaign_id)
                if source["project_id"] != project_id:
                    raise NotFound("Campaign was not found.")
                if source["status"] not in FINISHED:
                    raise Conflict("Only a failed, stopped or cancelled campaign can be re-run.")
                mode = body.mode or "failed_stages"
                request = RerunCampaign(
                    mode=mode,
                    stage_keys=body.stage_keys,
                    budget_cap_cents=(
                        body.per_run_budget_cap_cents if mode == "failed_stages" else None
                    ),
                )
                loaded = load_source(connection, source)
                try:
                    plan = plan_rerun(loaded, request)
                    spec = parse_spec(plan["spec"])
                except RerunRefused as error:
                    raise Conflict(str(error)) from None
                except ValidationError as error:
                    raise Conflict(
                        "The re-run spec is not valid: " + str(_invalid(error))
                    ) from None
                remaining = lineage_remaining(connection, source)
                if body.per_run_budget_cap_cents > remaining:
                    raise Conflict(
                        f"per_run_budget_cap_cents exceeds the {max(remaining, 0)} cents left "
                        "of the re-run tree's first cap."
                    )
                lineage = plan["lineage"]
                source_id = source["id"]
            digest_value = spec_digest(spec)
            if body.expected_spec_digest is not None and digest_value != body.expected_spec_digest:
                raise Conflict("Spec digest does not match expected_spec_digest.")
            self._check_references(connection, live, project_id, spec)
            normal = normalize(spec)
            largest = max((s.max_exposure_cents for s in normal.stages), default=0)
            cap = body.per_run_budget_cap_cents
            if cap > spec.budget_cap_cents or cap < largest:
                raise StoreError(
                    "per_run_budget_cap_cents must be at most the spec budget cap and at "
                    "least the largest stage max_exposure_cents."
                )
            created = _parse_utc(utc_now())
            try:
                after = _parse_utc(body.not_after)
            except ValueError:
                raise StoreError("not_after must be an ISO-8601 time with an offset.") from None
            if after <= created or after > created + timedelta(days=MAX_WINDOW_DAYS):
                raise StoreError(f"not_after must be within {MAX_WINDOW_DAYS} days from now.")
            provenance = decision_provenance(live)
            now = _utc_string(created)
            row = {
                "id": "submission_" + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project_id,
                "kind": body.kind,
                "spec_json": json.dumps(spec.model_dump(mode="json"), sort_keys=True),
                "spec_digest": digest_value,
                "source_campaign_id": source_id,
                "rerun_mode": None if lineage is None else lineage["mode"],
                "lineage_json": None if lineage is None else json.dumps(lineage, sort_keys=True),
                "created_by_principal_id": live["id"],
                "created_credential_id": live["credential_id"],
                "decision_class": provenance["decision_class"],
                "automation_label": provenance.get("automation_label"),
                "trust_policy_digest": provider_trust.current_policy(connection).digest,
                "per_run_budget_cap_cents": cap,
                "not_after": _utc_string(after),
                "status": "pending",
                "campaign_id": None,
                "approval_id": None,
                "last_refusal_reason": None,
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            names = ", ".join(row)
            marks = ", ".join(f":{k}" for k in row)
            connection.execute(
                f"INSERT INTO campaign_submission_approvals ({names}) VALUES ({marks})", row
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "campaign.submission_approval_created",
                1,
                {
                    "submission_approval_id": row["id"],
                    "kind": body.kind,
                    "spec_digest": digest_value,
                    "source_campaign_id": source_id,
                    "per_run_budget_cap_cents": cap,
                    "not_after": row["not_after"],
                    "trust_policy_digest": row["trust_policy_digest"],
                    **provenance,
                },
            )
            return 201, {**_summary(row), "spec": spec.model_dump(mode="json")}

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/submission-approvals", digest, command
        )

    # reads -----------------------------------------------------------------------------------

    def _reader(self, connection, principal) -> dict[str, Any]:
        actor = self._actor(connection, principal)
        self._require_action(connection, actor, READ_SCOPE)
        return actor

    def list_submission_approvals(
        self, principal, project_id, status=None, offset=0, limit=50
    ) -> dict[str, Any]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        if status is not None and status not in STATUSES:
            raise StoreError("Invalid status filter.")
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM campaign_submission_approvals WHERE project_id=? AND org_id=?
                AND (? IS NULL OR status=?) ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, actor["org_id"], status, status, limit, offset),
            ).fetchall()
            return {"submission_approvals": [_summary(r) for r in rows]}
        finally:
            connection.close()

    def get_submission_approval(self, principal, approval_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal)
            row = self._row(connection, actor, approval_id)
            return {
                **_summary(row),
                "spec": json.loads(row["spec_json"]),
                "decisions": _decisions(connection, approval_id),
            }
        finally:
            connection.close()

    # submit ----------------------------------------------------------------------------------

    def _checks(self, connection, live, row, body, now) -> tuple[Any, dict[str, Any] | None]:
        """Every check in order; raises SubmissionRefused, returns (spec, creator)."""

        def refuse(reason: str, details: str | None = None):
            return SubmissionRefused(
                reason, row["id"], revision=row["revision"], details=details, live=live
            )

        if now >= _parse_utc(row["not_after"]):
            raise refuse("expired")
        if row["revision"] != body.expected_revision:
            raise refuse("stale_revision")
        try:
            creator = Store._live_principal(
                connection,
                {"id": row["created_by_principal_id"], "org_id": row["org_id"]},
            )
        except Forbidden:
            raise refuse("creator_not_live") from None
        if creator["role"] != "human":
            raise refuse("creator_not_human")
        if (
            row["created_credential_id"] is not None
            and creator["credential_id"] != row["created_credential_id"]
        ):
            raise refuse("creator_credential_changed")
        try:
            Store._project(connection, creator, row["project_id"])
        except NotFound:
            raise refuse("creator_not_project_member") from None
        try:
            spec = parse_spec_json(row["spec_json"], providers=True)
        except Exception:  # noqa: BLE001 - any parse failure is a permanent invalid spec
            raise refuse("spec_invalid") from None
        if spec_digest(spec) != row["spec_digest"]:
            raise refuse("spec_digest_mismatch")
        if row["per_run_budget_cap_cents"] > spec.budget_cap_cents:
            raise refuse("per_run_cap_exceeds_spec")
        source = None
        if row["kind"] == "rerun":
            source = connection.execute(
                "SELECT * FROM campaigns WHERE id=? AND project_id=?",
                (row["source_campaign_id"], row["project_id"]),
            ).fetchone()
            if source is None or source["status"] not in FINISHED:
                raise refuse("rerun_source_not_finished")
        try:
            self._check_references(connection, creator, row["project_id"], spec)
            for stage in normalize(spec).stages:
                self.check_stage_inputs(
                    connection, stage, stage_provider(stage.provider), row["trust_policy_digest"]
                )
        except sqlite3.OperationalError:
            raise  # transient storage trouble is not a refusal
        except Exception as error:  # noqa: BLE001 - trust, data-class and reference refusals
            raise refuse("spec_references_invalid", f"{type(error).__name__}: {error}") from None
        reason = program_budget.check_admission(
            connection, row["project_id"], row["per_run_budget_cap_cents"]
        )
        if reason is not None:
            raise refuse(reason)
        headroom = _headroom(connection, row["project_id"])
        if headroom is None:
            raise refuse("no_project_spend_limit")
        if headroom < row["per_run_budget_cap_cents"]:
            raise refuse("project_spend_headroom")
        if source is not None and row["per_run_budget_cap_cents"] > lineage_remaining(
            connection, source
        ):
            raise refuse("rerun_budget_exceeds_lineage")
        return spec, creator

    def submit_approved(self, principal, approval_id, key, digest, payload, config=None):
        """Consume an approved submission: one campaign per approval, or a recorded refusal."""
        try:
            body = SubmitApprovedSubmission.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        config = config or AgentSubmissionConfig()

        def command(connection, live):
            self._require_scoped(connection, live, SUBMIT_SCOPE)
            row = self._row(connection, live, approval_id)
            if row["status"] == "submitted":
                # The approval was already consumed: answer with the same campaign.
                return 200, {**_summary(row), "replayed": True}
            if not config.enabled:
                raise SubmissionRefused(
                    "feature_disabled", approval_id, revision=row["revision"], live=live
                )
            if kill_switch_engaged(config):
                raise SubmissionRefused(
                    "kill_switch_engaged", approval_id, revision=row["revision"], live=live
                )
            if row["status"] != "pending":
                raise SubmissionRefused(
                    "not_pending", approval_id, revision=row["revision"], live=live, record=False
                )
            now_dt = _parse_utc(utc_now())
            spec, _creator = self._checks(connection, live, row, body, now_dt)
            campaign_id, campaign_approval_id, expires = insert_approved_campaign(
                connection,
                org_id=row["org_id"],
                project_id=row["project_id"],
                spec=spec,
                spec_json=row["spec_json"],
                spec_digest_value=row["spec_digest"],
                budget_cap_cents=row["per_run_budget_cap_cents"],
                principal_id=row["created_by_principal_id"],
                credential_id=row["created_credential_id"],
                trust_policy_digest=row["trust_policy_digest"],
                decision_class=CLASS_APPROVED_SUBMISSION,
                automation_label=f"approved_submission:{row['id']}",
                standing_approval_id=None,
                now=now_dt,
            )
            stamp = _utc_string(now_dt.replace(microsecond=0))
            revision = row["revision"] + 1
            connection.execute(
                """UPDATE campaign_submission_approvals SET status='submitted', campaign_id=?,
                approval_id=?, last_refusal_reason=NULL, revision=?, updated_at=?
                WHERE id=? AND status='pending'""",
                (campaign_id, campaign_approval_id, revision, stamp, row["id"]),
            )
            creator = {"id": row["created_by_principal_id"], "org_id": row["org_id"]}
            normal = normalize(spec)
            Store._event(
                connection,
                creator,
                row["project_id"],
                None,
                "campaign.submitted",
                1,
                {
                    "campaign_id": campaign_id,
                    "spec_digest": row["spec_digest"],
                    "stage_keys": [stage.key for stage in normal.stages],
                    "submission_approval_id": row["id"],
                },
            )
            if row["lineage_json"]:
                Store._event(
                    connection,
                    creator,
                    row["project_id"],
                    None,
                    RERUN_EVENT,
                    1,
                    {"campaign_id": campaign_id, **json.loads(row["lineage_json"])},
                )
            Store._event(
                connection,
                creator,
                row["project_id"],
                None,
                "campaign.approved",
                2,
                {
                    "campaign_id": campaign_id,
                    "approval_id": campaign_approval_id,
                    "spec_digest": row["spec_digest"],
                    "expires_at": expires,
                    "superseded_approval_id": None,
                    "resulting_status": "running",
                    "decision_class": CLASS_APPROVED_SUBMISSION,
                    "submission_approval_id": row["id"],
                    "submission_decision_class": row["decision_class"],
                },
            )
            _decide(
                connection,
                live,
                row,
                "submitted",
                None,
                {
                    "campaign_id": campaign_id,
                    "campaign_approval_id": campaign_approval_id,
                    "budget_cap_cents": row["per_run_budget_cap_cents"],
                },
                revision,
            )
            updated = connection.execute(
                "SELECT * FROM campaign_submission_approvals WHERE id=?", (row["id"],)
            ).fetchone()
            return 201, {**_summary(updated), "replayed": False}

        def replay_guard(connection, actor, response):
            self._require_scoped(connection, actor, SUBMIT_SCOPE)
            self._row(connection, actor, approval_id)

        try:
            return self._mutate(
                principal,
                key,
                "POST",
                f"/submission-approvals/{approval_id}/submit",
                digest,
                command,
                replay_guard=replay_guard,
            )
        except SubmissionRefused as refusal:
            if refusal.record:
                self._record_refusal(refusal)
            raise

    def _record_refusal(self, refusal: SubmissionRefused) -> None:
        """Record a refusal in its own transaction (the command's was rolled back, so nothing
        was created). Permanent refusals end the approval; a repeated temporary reason is not
        appended again. Recorded only while the approval is unchanged since the check."""
        live = refusal.live
        if live is None:
            return
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            recovery = connection.execute(
                "SELECT state FROM control_recovery WHERE singleton=1"
            ).fetchone()
            row = connection.execute(
                "SELECT * FROM campaign_submission_approvals WHERE id=?", (refusal.approval_id,)
            ).fetchone()
            if (
                recovery is None
                or recovery["state"] != "active"
                or row is None
                or row["status"] != "pending"
                or row["revision"] != refusal.revision
            ):
                connection.execute("ROLLBACK")
                return
            stamp = utc_now()
            details = {"permanent": refusal.permanent}
            if refusal.details:
                details["details"] = refusal.details
            if refusal.permanent:
                status = "expired" if refusal.reason == "expired" else "refused_permanent"
                revision = row["revision"] + 1
                connection.execute(
                    """UPDATE campaign_submission_approvals SET status=?, last_refusal_reason=?,
                    revision=?, updated_at=? WHERE id=?""",
                    (status, refusal.reason, revision, stamp, row["id"]),
                )
                decision = "expired" if status == "expired" else "refused"
                _decide(connection, live, row, decision, refusal.reason, details, revision)
            elif row["last_refusal_reason"] != refusal.reason:
                connection.execute(
                    "UPDATE campaign_submission_approvals SET last_refusal_reason=? WHERE id=?",
                    (refusal.reason, row["id"]),
                )
                _decide(connection, live, row, "refused", refusal.reason, details, row["revision"])
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            log.error("submission refusal not recorded: %s", type(error).__name__)
        finally:
            connection.close()

    # cancel ----------------------------------------------------------------------------------

    def cancel_submission_approval(self, principal, approval_id, key, digest, payload):
        """Reduce-only: a pending approval is withdrawn. Humans, or agents with campaign:cancel."""
        try:
            body = CancelSubmissionApproval.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._require_scoped(connection, live, CANCEL_SCOPE)
            row = self._row(connection, live, approval_id)
            if row["revision"] != body.expected_revision:
                raise Conflict("Submission approval revision is no longer current.")
            if row["status"] != "pending":
                raise Conflict(f"Submission approval is already {row['status']}.")
            stamp, revision = utc_now(), row["revision"] + 1
            connection.execute(
                """UPDATE campaign_submission_approvals SET status='cancelled', revision=?,
                updated_at=? WHERE id=? AND status='pending'""",
                (revision, stamp, row["id"]),
            )
            _decide(connection, live, row, "cancelled", body.reason, {}, revision)
            updated = connection.execute(
                "SELECT * FROM campaign_submission_approvals WHERE id=?", (row["id"],)
            ).fetchone()
            return 200, _summary(updated)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/submission-approvals/{approval_id}/cancel",
            digest,
            command,
        )


__all__ = [
    "CANCEL_SCOPE",
    "CLASS_APPROVED_SUBMISSION",
    "PERMANENT_REASONS",
    "SUBMIT_SCOPE",
    "AgentSubmissionConfig",
    "CampaignSubmissionStore",
    "CancelSubmissionApproval",
    "CreateSubmissionApproval",
    "SubmissionRefused",
    "SubmitApprovedSubmission",
    "create_submission_schema",
    "kill_switch_engaged",
    "submission_receipt_section",
]
