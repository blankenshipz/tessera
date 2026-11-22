"""Steering commands an enrolled agent may use on its own campaigns (ADR 0055).

Everything here either changes nothing (``validate_campaign_spec``) or only REDUCES authority:
``reduce_campaign_budget`` lowers an approved cap, ``revoke_campaign_release`` withdraws a data
release a finished campaign no longer needs. Neither can approve, raise a cap, release data or
stage anything. They are gated by the same enrolled actions and submitter-only rule as
``cancel_campaign``; a human may use them too.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError

from .campaign_models import (
    TERMINAL_CAMPAIGN,
    SubmitCampaign,
    _Strict,
    normalize,
    spec_digest,
    stage_input_class,
    stage_output_class,
    topological_order,
)
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, utc_now

Reason = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class ReduceCampaignBudget(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    budget_cap_cents: Annotated[int, Field(strict=True, ge=0)]
    reason: Reason


class RevokeCampaignRelease(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Reason


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid campaign request: {location}: {str(first.get('msg', ''))[:200]}")


class CampaignSteeringCommands:
    """Mixed into ``CampaignStore``; relies on its actor, scope, campaign and mutate helpers."""

    def validate_campaign_spec(self, principal, project_id, payload) -> dict[str, Any]:
        """Dry-run of submit: the same parse and reference checks, and nothing is written."""
        try:
            body = SubmitCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        spec = body.spec
        normal = normalize(spec)
        connection = _connect(self.database_path)
        try:
            live = self._actor(connection, principal)
            self._project(connection, live, project_id)
            self._require_action(connection, live, "campaign:submit")
            if live["role"] not in {"human", "agent"}:
                raise Forbidden("This credential cannot submit campaigns.")
            self._check_references(connection, live, project_id, spec)
            order = topological_order(spec)
            return {
                "valid": True,
                "project_id": project_id,
                "spec_digest": spec_digest(spec),
                "version": spec.version,
                "budget_cap_cents": spec.budget_cap_cents,
                "stage_order": order,
                "stages": [
                    {
                        "key": key,
                        "provider": normal.stage(key).provider,
                        "input_data_class": stage_input_class(spec, key),
                        "output_data_class": stage_output_class(spec, key),
                        "max_exposure_cents": normal.stage(key).max_exposure_cents,
                    }
                    for key in order
                ],
                "boundary": (
                    "Validation only: nothing was stored or submitted, and a human must still "
                    "approve the digest of the spec that is submitted."
                ),
            }
        finally:
            connection.close()

    def _own_campaign_for_reduction(self, connection, live, campaign_id) -> sqlite3.Row:
        campaign = self._campaign(connection, live, campaign_id)
        if self._external_scope(connection, live):
            self._scope(connection, live, "campaign:cancel")
            if campaign["submitted_by_principal_id"] != live["id"]:
                raise Forbidden("Only the submitting agent may reduce this campaign.")
        elif live["role"] != "human":
            raise Forbidden("A human must reduce a campaign.")
        return campaign

    def reduce_campaign_budget(self, principal, campaign_id, key, digest, payload):
        """Lower an in-flight campaign's budget cap. Never raises it (reduce-only)."""
        try:
            body = ReduceCampaignBudget.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._own_campaign_for_reduction(connection, live, campaign_id)
            if campaign["status"] in TERMINAL_CAMPAIGN or campaign["status"] == "submitted":
                raise Conflict("Only an approved, unfinished campaign's cap can be reduced.")
            if campaign["revision"] != body.expected_revision:
                raise Conflict("Campaign revision is no longer current.")
            if body.budget_cap_cents >= campaign["budget_cap_cents"]:
                raise Conflict("A cap can only be reduced; raising it needs a new approval.")
            committed = campaign["held_cents"] + campaign["settled_cents"]
            if body.budget_cap_cents < committed:
                raise Conflict(
                    f"The cap cannot go below what is already held and settled ({committed})."
                )
            now, revision = utc_now(), campaign["revision"] + 1
            connection.execute(
                "UPDATE campaigns SET budget_cap_cents=?, revision=?, updated_at=? WHERE id=?",
                (body.budget_cap_cents, revision, now, campaign_id),
            )
            self._event(
                connection,
                live,
                campaign["project_id"],
                None,
                "campaign.budget_reduced",
                revision,
                {
                    "campaign_id": campaign_id,
                    "from_cents": campaign["budget_cap_cents"],
                    "to_cents": body.budget_cap_cents,
                    "reason": body.reason,
                },
            )
            return 200, {
                "campaign_id": campaign_id,
                "budget_cap_cents": body.budget_cap_cents,
                "previous_budget_cap_cents": campaign["budget_cap_cents"],
                "held_cents": campaign["held_cents"],
                "settled_cents": campaign["settled_cents"],
                "revision": revision,
                "updated_at": now,
            }

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/budget-reduction", digest, command
        )

    def revoke_campaign_release(self, principal, campaign_id, release_id, key, digest, payload):
        """Withdraw a data release one of this campaign's attempts used, once it has finished."""
        try:
            body = RevokeCampaignRelease.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._own_campaign_for_reduction(connection, live, campaign_id)
            if campaign["status"] not in TERMINAL_CAMPAIGN | {"cancel_requested"}:
                raise Conflict("A release is revoked only after the campaign has finished.")
            used = connection.execute(
                "SELECT 1 FROM campaign_stage_attempts WHERE campaign_id=? AND release_id=?",
                (campaign_id, release_id),
            ).fetchone()
            release = connection.execute(
                "SELECT * FROM snapshot_releases WHERE id=?", (release_id,)
            ).fetchone()
            if used is None or release is None:
                raise NotFound("Release was not found on this campaign.")
            if release["revision"] != body.expected_revision or release["status"] != "active":
                raise Conflict("Snapshot release revision is stale.")
            now = utc_now()
            connection.execute(
                """UPDATE snapshot_releases SET status='revoked', revision=revision+1,
                revoked_at=? WHERE id=?""",
                (now, release_id),
            )
            self._event(
                connection,
                live,
                campaign["project_id"],
                None,
                "workspace.snapshot_release_revoked",
                body.expected_revision + 1,
                {
                    "release_id": release_id,
                    "snapshot_id": release["snapshot_id"],
                    "campaign_id": campaign_id,
                    "reason": body.reason,
                },
            )
            return 200, {
                "release_id": release_id,
                "snapshot_id": release["snapshot_id"],
                "status": "revoked",
                "revoked_at": now,
                "revision": body.expected_revision + 1,
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/campaigns/{campaign_id}/releases/{release_id}/revoke",
            digest,
            command,
        )
