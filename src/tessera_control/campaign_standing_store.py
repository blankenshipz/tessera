"""Human-only standing-approval commands and reads (ADR 0042 sections 1, 5 and 7)."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from pydantic import ValidationError

from . import provider_trust
from .approval_trust import decision_provenance, require_decision_actor
from .campaign_io import refuse_submitted_lineage
from .campaign_models import normalize, spec_digest
from .campaign_salad_adapter import expand_with_registered
from .campaign_standing import (
    MAX_WINDOW_DAYS,
    CreateStandingApproval,
    RevokeStandingApproval,
    abandon_triggers,
    running_campaigns,
)
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    utc_now,
)


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid standing approval: {location}: {str(first.get('msg', ''))[:200]}")


def _summary(row) -> dict[str, Any]:
    return {k: v for k, v in dict(row).items() if k != "spec_json"}


def _timestamp(value: str, label: str):
    try:
        return _parse_utc(value)
    except ValueError:
        raise StoreError(f"{label} must be an ISO-8601 time with an offset.") from None


class StandingApprovalCommands:
    """Mixed into CampaignStore; relies on its _mutate, _actor and _check_references."""

    def _require_human(self, connection: sqlite3.Connection, actor: dict[str, Any]) -> None:
        """Agents, workers and external-scope credentials never reach standing approvals."""
        if actor.get("kind") == "worker" or actor["role"] != "human":
            raise Forbidden("A human must manage standing approvals.")
        if self._external_scope(connection, actor):
            raise Forbidden("A human must manage standing approvals.")

    @staticmethod
    def _standing(connection, actor, project_id: str, standing_id: str) -> sqlite3.Row:
        row = connection.execute(
            """SELECT a.* FROM campaign_standing_approvals a JOIN projects p ON p.id=a.project_id
            WHERE a.id=? AND a.project_id=? AND p.org_id=?""",
            (standing_id, project_id, actor["org_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Standing approval was not found.")
        return row

    def create_standing_approval(self, principal, project_id, key, digest, payload):
        try:
            body = CreateStandingApproval.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        raw_spec = payload.get("spec") if isinstance(payload, dict) else None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_human(connection, live)
            require_decision_actor(live, forbidden=Forbidden)
            refuse_submitted_lineage(raw_spec)
            # ADR 0077: the same expansion submit performs, so an adapter request is never
            # checked raw; the expected digest is the digest of the expanded spec.
            spec = expand_with_registered(
                body.spec,
                self.adapter_entries(connection, live, project_id),
                (live["org_id"], project_id),
            )
            if any(
                getattr(source, "kind", None) == "receipt_output"
                for stage in spec.stages
                for source in getattr(stage, "inputs", [])
            ):
                raise Conflict(
                    "A standing approval cannot use receipt_output inputs: pin the value with "
                    "an artifact input (campaign_id, stage, output, sha256) instead."
                )
            if spec_digest(spec) != body.expected_spec_digest:
                raise Conflict("Spec digest does not match expected_spec_digest.")
            self._check_references(connection, live, project_id, spec)
            normal = normalize(spec)
            largest = max((s.max_exposure_cents for s in normal.stages), default=0)
            cap = body.per_start_budget_cap_cents
            if cap > spec.budget_cap_cents or cap < largest:
                raise StoreError(
                    "per_start_budget_cap_cents must be at most the spec budget cap and at "
                    "least the largest stage max_exposure_cents."
                )
            if body.max_total_budget_cents < cap:
                raise StoreError("max_total_budget_cents must be at least the per-start cap.")
            created = _parse_utc(utc_now())
            after = _timestamp(body.not_after, "not_after")
            before = _timestamp(body.not_before, "not_before") if body.not_before else None
            if after <= created or after > created + timedelta(days=MAX_WINDOW_DAYS):
                raise StoreError(f"not_after must be within {MAX_WINDOW_DAYS} days from now.")
            if before is not None and before >= after:
                raise StoreError("not_before must precede not_after.")
            follow = body.follow_on_standing_approval_id
            if follow is not None:
                target = self._standing(connection, live, project_id, follow)
                if target["status"] != "active" or target["trigger"] != "follow_on":
                    raise Conflict("The follow-on must be an active follow_on standing approval.")
            unlock = body.unlock or []
            self.check_unlock_references(connection, live["org_id"], project_id, unlock)
            ident = "standing_" + uuid.uuid4().hex
            if follow == ident:
                raise Conflict("A standing approval cannot follow itself.")
            provenance = decision_provenance(live)
            now = _utc_string(created)
            row = {
                "id": ident,
                "org_id": live["org_id"],
                "project_id": project_id,
                "spec_json": json.dumps(spec.model_dump(mode="json"), sort_keys=True),
                "spec_digest": body.expected_spec_digest,
                "created_by_principal_id": live["id"],
                "created_credential_id": live["credential_id"],
                "decision_class": provenance["decision_class"],
                "automation_label": provenance.get("automation_label"),
                "trust_policy_digest": provider_trust.current_policy(connection).digest,
                "per_start_budget_cap_cents": cap,
                "max_starts": body.max_starts,
                "max_total_budget_cents": body.max_total_budget_cents,
                "min_interval_seconds": body.min_interval_seconds,
                "max_concurrent": body.max_concurrent,
                "trigger": body.trigger,
                "follow_on_standing_approval_id": follow,
                "retry_max": body.retry_max,
                "retry_on": body.retry_on,
                "not_before": _utc_string(before) if before else None,
                "not_after": _utc_string(after),
                "unlock_json": (
                    json.dumps([u.model_dump(mode="json") for u in unlock], sort_keys=True)
                    if unlock
                    else None
                ),
                "status": "active",
                "revision": 1,
                "created_at": now,
                "updated_at": now,
            }
            names = ", ".join(f'"{k}"' for k in row)
            marks = ", ".join(f":{k}" for k in row)
            connection.execute(
                f"INSERT INTO campaign_standing_approvals ({names}) VALUES ({marks})", row
            )
            self._event(
                connection,
                live,
                project_id,
                None,
                "campaign.standing_approval_created",
                1,
                {
                    "standing_approval_id": ident,
                    "spec_digest": row["spec_digest"],
                    "per_start_budget_cap_cents": cap,
                    "max_starts": body.max_starts,
                    "max_total_budget_cents": body.max_total_budget_cents,
                    "trigger": body.trigger,
                    "unlock_requirements": len(unlock),
                    "not_after": row["not_after"],
                    "trust_policy_digest": row["trust_policy_digest"],
                },
            )
            return 201, {
                **_summary({**row, "starts_used": 0, "committed_budget_cents": 0}),
                "spec": spec.model_dump(mode="json"),
            }

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/standing-approvals", digest, command
        )

    def revoke_standing_approval(self, principal, project_id, standing_id, key, digest, payload):
        try:
            body = RevokeStandingApproval.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_human(connection, live)
            require_decision_actor(live, forbidden=Forbidden)
            row = self._standing(connection, live, project_id, standing_id)
            if row["revision"] != body.expected_revision:
                raise Conflict("Standing approval revision is no longer current.")
            if row["status"] != "active":
                raise Conflict(f"Standing approval is already {row['status']}.")
            now, revision = utc_now(), row["revision"] + 1
            connection.execute(
                """UPDATE campaign_standing_approvals SET status='revoked', revoked_at=?,
                revoked_by_principal_id=?, revoke_reason=?, revision=?, updated_at=? WHERE id=?""",
                (now, live["id"], body.reason, revision, now, standing_id),
            )
            abandoned = abandon_triggers(connection, row, "target_revoked", now)
            running = running_campaigns(connection, standing_id)
            self._event(
                connection,
                live,
                project_id,
                None,
                "campaign.standing_approval_revoked",
                revision,
                {
                    "standing_approval_id": standing_id,
                    "reason": body.reason,
                    "running_campaign_ids": running,
                    "abandoned_trigger_campaign_ids": abandoned,
                },
            )
            return 200, {
                **_summary(row),
                "status": "revoked",
                "revision": revision,
                "revoked_at": now,
                "revoke_reason": body.reason,
                "running_campaign_ids": running,
                "abandoned_trigger_campaign_ids": abandoned,
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/standing-approvals/{standing_id}/revoke",
            digest,
            command,
        )

    def _standing_actor(self, connection, principal) -> dict[str, Any]:
        actor = self._actor(connection, principal)
        self._require_human(connection, actor)
        return actor

    def list_standing_approvals(self, principal, project_id, offset=0, limit=50) -> dict:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            actor = self._standing_actor(connection, principal)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT * FROM campaign_standing_approvals WHERE project_id=? AND org_id=?
                ORDER BY created_at, id LIMIT ? OFFSET ?""",
                (project_id, actor["org_id"], limit, offset),
            ).fetchall()
            return {"standing_approvals": [_summary(r) for r in rows]}
        finally:
            connection.close()

    def get_standing_approval(self, principal, project_id, standing_id) -> dict:
        connection = _connect(self.database_path)
        try:
            actor = self._standing_actor(connection, principal)
            self._project(connection, actor, project_id)
            row = self._standing(connection, actor, project_id, standing_id)
            starts = connection.execute(
                "SELECT * FROM campaign_standing_starts WHERE standing_approval_id=? "
                "ORDER BY start_index",
                (standing_id,),
            ).fetchall()
            triggers = connection.execute(
                "SELECT * FROM campaign_standing_triggers WHERE target_standing_approval_id=? "
                "ORDER BY created_at, trigger_campaign_id, kind",
                (standing_id,),
            ).fetchall()
            return {
                **_summary(row),
                "spec": json.loads(row["spec_json"]),
                "starts": [dict(r) for r in starts],
                "triggers": [dict(r) for r in triggers],
            }
        finally:
            connection.close()


__all__ = ["StandingApprovalCommands"]
