"""Steering commands an enrolled agent may use on its own campaigns (ADR 0055).

Everything here either changes nothing (``validate_campaign_spec``) or only REDUCES authority:
``reduce_campaign_budget`` lowers an approved cap, ``revoke_campaign_release`` withdraws a data
release a finished campaign no longer needs. Neither can approve, raise a cap, release data or
stage anything. They are gated by the same enrolled actions and submitter-only rule as
``cancel_campaign``; a human may use them too.

``steer_campaign_placement`` (ADR 0060) is a filter over a stage's approved provider candidates.
The latest call replaces the previous one, so it can re-allow a candidate excluded earlier, but it
can never name a provider the approved spec does not list.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError

from . import campaign_heavy_remote as heavy_remote
from . import campaign_image_pull, smoke_credentials
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
from .campaign_salad_adapter import expand_with_registered
from .store import Conflict, Forbidden, NotFound, StoreError, _connect, utc_now

Reason = Annotated[str, StringConstraints(min_length=1, max_length=1000)]
# ADR 0061: only a campaign the runner is actively driving can be paused, limited or have a
# stage cancelled; a delegation-lapsed, blocked or finished one needs a person (or is over).
STEERABLE = frozenset({"running", "stopping"})
MAX_CONCURRENT_LIMIT = 64
STAGE_FINISHED = frozenset(
    {"succeeded", "failed", "refused_budget", "blocked_unknown", "cancelled", "skipped"}
)


class ReduceCampaignBudget(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    budget_cap_cents: Annotated[int, Field(strict=True, ge=0)]
    reason: Reason


class SteerCampaignPlacement(_Strict):
    """ADR 0060: pin or exclude approved provider candidates of one stage. It filters the
    approved list for later placements and can never add a provider the spec does not name."""

    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    stage_key: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    pin: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")] | None = None
    exclude: Annotated[
        list[Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,63}$")]],
        Field(max_length=8),
    ] = []  # noqa: RUF012 - pydantic copies defaults
    reason: Reason


class RevokeCampaignRelease(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Reason


class PauseCampaign(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Reason


class ResumeCampaign(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Reason


class LimitCampaignConcurrency(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    max_concurrent_stages: Annotated[int, Field(strict=True, ge=1, le=MAX_CONCURRENT_LIMIT)]
    reason: Reason


class CancelCampaignStage(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]
    reason: Reason


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid campaign request: {location}: {str(first.get('msg', ''))[:200]}")


def _image_pull_plan(stage: Any) -> dict[str, Any]:
    """The stage's ``image_pull_preflight`` plan, absent when no candidate has a ceiling."""
    sections = campaign_image_pull.plan(stage)
    return {campaign_image_pull.SECTION: sections} if sections else {}


class CampaignSteeringCommands:
    """Mixed into ``CampaignStore``; relies on its actor, scope, campaign and mutate helpers."""

    def validate_campaign_spec(self, principal, project_id, payload) -> dict[str, Any]:
        """Dry-run of submit: the same parse and reference checks, and nothing is written."""
        try:
            body = SubmitCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        raw_spec = payload.get("spec") if isinstance(payload, dict) else None
        connection = _connect(self.database_path)
        try:
            live = self._actor(connection, principal)
            self._project(connection, live, project_id)
            self._require_action(connection, live, "campaign:submit")
            if live["role"] not in {"human", "agent"}:
                raise Forbidden("This credential cannot submit campaigns.")
            # ADR 0076/0077: the same expansion submit performs; the identity without an adapter.
            # ADR 0123: image_from becomes the built image before expansion and the digest.
            image_spec, used_predecessors = self._resolve_image_refs(
                connection, live, project_id, body.spec, body.predecessors
            )
            spec = expand_with_registered(
                image_spec,
                self.adapter_entries(connection, live, project_id),
                tenant=(live["org_id"], project_id),
            )
            # ADR 0080: resolve receipt_output references read-only, exactly as submit does.
            spec = self._resolve_receipt_refs(
                connection,
                live,
                project_id,
                spec,
                body.predecessors,
                raw_spec,
                used_predecessors,
            )
            # ADR 0141: the same heavy-stage candidate submit adds, so the digests agree.
            spec = heavy_remote.apply(
                self,
                connection,
                live,
                project_id,
                spec,
                self.adapter_entries(connection, live, project_id),
            )
            normal = normalize(spec)
            smoke_credentials.check_submission(connection, live, normal)
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
                        # The image pull-size preflight per candidate provider, before dispatch.
                        **_image_pull_plan(normal.stage(key)),
                    }
                    for key in order
                ],
                **(
                    {"expanded_spec": spec.model_dump(mode="json")} if spec is not body.spec else {}
                ),
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

    def steer_campaign_placement(self, principal, campaign_id, key, digest, payload):
        """Pin or exclude provider candidates of a stage (ADR 0060). Applies to the stage's
        next placement; an attempt already placed keeps its provider. The latest call replaces the
        previous one; only candidates the approved spec names can be pinned or excluded, and at
        least one must remain."""
        from .campaign_models import parse_spec_json
        from .campaign_placement import STEERED_EVENT, allowed_after_steering

        try:
            body = SteerCampaignPlacement.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._own_campaign_for_reduction(connection, live, campaign_id)
            if campaign["status"] in TERMINAL_CAMPAIGN or campaign["status"] == "submitted":
                raise Conflict("Only an approved, unfinished campaign can be steered.")
            if campaign["revision"] != body.expected_revision:
                raise Conflict("Campaign revision is no longer current.")
            normal = normalize(parse_spec_json(campaign["spec_json"]))
            stage = next((s for s in normal.stages if s.key == body.stage_key), None)
            if stage is None:
                raise NotFound("Stage was not found on this campaign.")
            if not stage.has_candidates:
                raise Conflict("This stage names one provider; there is nothing to steer.")
            named = set(stage.candidate_ids)
            unknown = sorted({*body.exclude, *([body.pin] if body.pin else [])} - named)
            if unknown:
                raise Conflict(
                    f"Only the stage's approved candidates can be steered; not {unknown}."
                )
            reroute_only = set(stage.reroute_ids) - set(stage.placement_ids)
            if body.pin in reroute_only:
                raise Conflict(
                    f"{body.pin} is a reroute target (ADR 0058): it serves only infrastructure "
                    "retries and cannot be pinned."
                )
            allowed = allowed_after_steering(stage.candidate_ids, body.pin, list(body.exclude))
            if not [pid for pid in allowed if pid not in reroute_only]:
                raise Conflict(
                    "Steering would leave the stage no candidate; cancel the campaign instead."
                )
            now, revision = utc_now(), campaign["revision"] + 1
            connection.execute(
                "UPDATE campaigns SET revision=?, updated_at=? WHERE id=?",
                (revision, now, campaign_id),
            )
            steering = {"pin": body.pin, "exclude": sorted(set(body.exclude))}
            self._event(
                connection,
                live,
                campaign["project_id"],
                None,
                STEERED_EVENT,
                revision,
                {
                    "campaign_id": campaign_id,
                    "stage_key": body.stage_key,
                    **steering,
                    "allowed": allowed,
                    "reason": body.reason,
                },
            )
            return 200, {
                "campaign_id": campaign_id,
                "stage_key": body.stage_key,
                "candidates": list(stage.candidate_ids),
                "steering": steering,
                "allowed": allowed,
                "revision": revision,
                "updated_at": now,
                "boundary": (
                    "Applies to the stage's next placement; an attempt already placed keeps "
                    "its provider. This replaces any earlier steering of the stage and only "
                    "filters the approved candidates."
                ),
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/campaigns/{campaign_id}/placement-steering",
            digest,
            command,
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

    # -- pause, concurrency and single-stage cancel (ADR 0061) ---------------------------------

    def _steerable(self, connection, live, campaign_id, expected_revision) -> sqlite3.Row:
        campaign = self._own_campaign_for_reduction(connection, live, campaign_id)
        if campaign["status"] not in STEERABLE:
            raise Conflict("Only a running campaign can be steered this way.")
        if campaign["revision"] != expected_revision:
            raise Conflict("Campaign revision is no longer current.")
        return campaign

    @staticmethod
    def _steering_row(connection, campaign_id) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM campaign_steering WHERE campaign_id=?", (campaign_id,)
        ).fetchone()

    def _bump(self, connection, live, campaign, event_type, payload) -> tuple[int, str]:
        now, revision = utc_now(), campaign["revision"] + 1
        connection.execute(
            "UPDATE campaigns SET revision=?, updated_at=? WHERE id=?",
            (revision, now, campaign["id"]),
        )
        self._event(
            connection,
            live,
            campaign["project_id"],
            None,
            event_type,
            revision,
            {"campaign_id": campaign["id"], **payload},
        )
        return revision, now

    def pause_campaign(self, principal, campaign_id, key, digest, payload):
        """Stop NEW stage reservations; running attempts, settlement and cancel are unaffected."""
        try:
            body = PauseCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._steerable(connection, live, campaign_id, body.expected_revision)
            row = self._steering_row(connection, campaign_id)
            if row is not None and row["paused"]:
                raise Conflict("The campaign is already paused.")
            now = utc_now()
            connection.execute(
                """INSERT INTO campaign_steering (campaign_id, org_id, project_id, paused,
                paused_by_role, paused_reason, paused_at, pause_count, updated_at)
                VALUES (?,?,?,1,?,?,?,1,?) ON CONFLICT(campaign_id) DO UPDATE SET paused=1,
                paused_by_role=excluded.paused_by_role, paused_reason=excluded.paused_reason,
                paused_at=excluded.paused_at, pause_count=pause_count+1,
                updated_at=excluded.updated_at""",
                (
                    campaign_id,
                    campaign["org_id"],
                    campaign["project_id"],
                    live["role"],
                    body.reason,
                    now,
                    now,
                ),
            )
            revision, now = self._bump(
                connection,
                live,
                campaign,
                "campaign.paused",
                {"reason": body.reason, "paused_by_role": live["role"]},
            )
            return 200, {"campaign_id": campaign_id, "paused": True, "revision": revision}

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/pause", digest, command
        )

    def resume_campaign(self, principal, campaign_id, key, digest, payload):
        """Lift a pause. An agent cannot lift a pause a person set."""
        try:
            body = ResumeCampaign.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._steerable(connection, live, campaign_id, body.expected_revision)
            row = self._steering_row(connection, campaign_id)
            if row is None or not row["paused"]:
                raise Conflict("The campaign is not paused.")
            if row["paused_by_role"] == "human" and live["role"] != "human":
                raise Forbidden("A pause set by a person can be lifted only by a person.")
            connection.execute(
                "UPDATE campaign_steering SET paused=0, updated_at=? WHERE campaign_id=?",
                (utc_now(), campaign_id),
            )
            revision, _ = self._bump(
                connection,
                live,
                campaign,
                "campaign.resumed",
                {"reason": body.reason, "resumed_by_role": live["role"]},
            )
            return 200, {"campaign_id": campaign_id, "paused": False, "revision": revision}

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/resume", digest, command
        )

    def limit_campaign_concurrency(self, principal, campaign_id, key, digest, payload):
        """Cap how many stages may have an unfinished attempt at once. Lower-only."""
        try:
            body = LimitCampaignConcurrency.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._steerable(connection, live, campaign_id, body.expected_revision)
            row = self._steering_row(connection, campaign_id)
            current = None if row is None else row["max_concurrent_stages"]
            if current is not None and body.max_concurrent_stages >= current:
                raise Conflict(
                    "A concurrency limit can only be lowered; "
                    "raising it needs the operator's project limits."
                )
            now = utc_now()
            connection.execute(
                """INSERT INTO campaign_steering (campaign_id, org_id, project_id,
                max_concurrent_stages, updated_at) VALUES (?,?,?,?,?)
                ON CONFLICT(campaign_id) DO UPDATE SET
                max_concurrent_stages=excluded.max_concurrent_stages,
                updated_at=excluded.updated_at""",
                (
                    campaign_id,
                    campaign["org_id"],
                    campaign["project_id"],
                    body.max_concurrent_stages,
                    now,
                ),
            )
            revision, _ = self._bump(
                connection,
                live,
                campaign,
                "campaign.concurrency_limited",
                {
                    "from": current,
                    "to": body.max_concurrent_stages,
                    "reason": body.reason,
                },
            )
            return 200, {
                "campaign_id": campaign_id,
                "max_concurrent_stages": body.max_concurrent_stages,
                "previous_max_concurrent_stages": current,
                "revision": revision,
            }

        return self._mutate(
            principal, key, "POST", f"/campaigns/{campaign_id}/concurrency-limit", digest, command
        )

    def cancel_campaign_stage(self, principal, campaign_id, stage_key, key, digest, payload):
        """Ask the runner to unwind one unfinished stage; its dependents are then skipped."""
        try:
            body = CancelCampaignStage.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            campaign = self._steerable(connection, live, campaign_id, body.expected_revision)
            stage = connection.execute(
                "SELECT status FROM campaign_stages WHERE campaign_id=? AND stage_key=?",
                (campaign_id, stage_key),
            ).fetchone()
            if stage is None:
                raise NotFound("Campaign stage was not found.")
            if stage["status"] in STAGE_FINISHED:
                raise Conflict("That stage has already finished.")
            if connection.execute(
                "SELECT 1 FROM campaign_stage_cancels WHERE campaign_id=? AND stage_key=?",
                (campaign_id, stage_key),
            ).fetchone():
                raise Conflict("A cancel is already requested for that stage.")
            connection.execute(
                """INSERT INTO campaign_stage_cancels (campaign_id, stage_key, org_id,
                project_id, reason, requested_by_principal_id, requested_at)
                VALUES (?,?,?,?,?,?,?)""",
                (
                    campaign_id,
                    stage_key,
                    campaign["org_id"],
                    campaign["project_id"],
                    body.reason,
                    live["id"],
                    utc_now(),
                ),
            )
            revision, _ = self._bump(
                connection,
                live,
                campaign,
                "campaign.stage_cancel_requested",
                {"stage_key": stage_key, "reason": body.reason},
            )
            return 200, {
                "campaign_id": campaign_id,
                "stage_key": stage_key,
                "cancel_requested": True,
                "revision": revision,
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/campaigns/{campaign_id}/stages/{stage_key}/cancel",
            digest,
            command,
        )
