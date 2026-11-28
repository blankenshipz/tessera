"""Operator recovery of unknown attempts that provably claimed no provider create (ADR 0205).

The runner reconciles such an attempt on its own (ADR 0157), at most
``--campaign-unclaimed-autoreconcile-limit`` times per stage (and that times the stage count per
campaign, ADR 0216); past that, or for an attempt
that became ``unknown`` under an earlier build, the campaign stays ``blocked``. The operator
route for the grant (``reconcile-unclaimed-salad``) releases the hold, but the runner then
settles the attempt ``failed_permanent``, which ends the stage.

:class:`UnclaimedRecovery` is the sanctioned recovery (``tessera campaign
reconcile-unclaimed-attempts``). The owner names a campaign; for each of its ``unknown``
attempts the same pure ``campaign_unclaimed.assess`` the runner uses decides, and for each one
it accepts one audited ``campaign.unclaimed_reconcile_requested`` event is written, bound to
that verdict's evidence digest. On its next pass the runner applies its own unclaimed unwind
to exactly those attempts, the per-campaign automatic limit aside, every guard re-read in its
own transaction: the grant reconciled at zero (hold released), the attempt
``failed_transient`` / ``not_started`` settled ``unclaimed_zero``, the stage re-queued under
its own caps (no scientific retry), and a ``blocked`` campaign with no other unknown attempt
back to ``running``. Nothing is settled here; no termination, charge or provider fact is
inferred; any refusal names the closed reason and writes nothing. A replay with the same
idempotency key returns the recorded response; a new request while the runner has not acted yet
finds the attempts ``requested`` and writes nothing new; once the runner has acted no unknown
attempt is left, so a new request is refused (409) like any request with nothing to reconcile.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .campaign_models import TERMINAL_CAMPAIGN
from .campaign_unclaimed import Refused, assess
from .store import Conflict, Forbidden, StoreError, _connect

REQUEST_EVENT = "campaign.unclaimed_reconcile_requested"
PROVIDER = "salad"


class ReconcileUnclaimedAttempts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str = Field(min_length=1, max_length=300)
    dry_run: bool = False


def requested(
    connection: sqlite3.Connection, campaign: Mapping[str, Any], attempt_id: str, digest: str
) -> str | None:
    """The requesting principal when the owner asked for this attempt at this evidence digest."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE project_id=? AND event_type=?
        AND json_extract(payload_json,'$.campaign_id')=?
        AND json_extract(payload_json,'$.attempt_id')=?
        AND json_extract(payload_json,'$.evidence_digest')=?
        ORDER BY cursor DESC LIMIT 1""",
        (campaign["project_id"], REQUEST_EVENT, campaign["id"], attempt_id, digest),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])["requested_by_principal_id"]


def _provider(connection: sqlite3.Connection, attempt: Mapping[str, Any]) -> Any:
    """The Salad provider for an attempt with a Salad record; None (``no_claim_hook``) else."""
    from .campaign_providers import get_provider

    row = connection.execute(
        "SELECT 1 FROM campaign_salad_attempts WHERE attempt_id=?", (attempt["id"],)
    ).fetchone()
    if row is None:
        return None
    try:
        return get_provider(PROVIDER)
    except KeyError:
        return None


def assess_campaign(
    connection: sqlite3.Connection, campaign: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Each ``unknown`` attempt of the campaign with its verdict (a pure read).

    ``verdict`` is ``reconcilable``, ``requested`` (already asked at the same evidence) or
    ``refused`` (with the closed ``refusal`` code and its sentence)."""
    entries = []
    for attempt in connection.execute(
        """SELECT * FROM campaign_stage_attempts WHERE campaign_id=? AND status='unknown'
        ORDER BY stage_key, attempt""",
        (campaign["id"],),
    ).fetchall():
        entry: dict[str, Any] = {
            "campaign_attempt_id": attempt["id"],
            "stage_key": attempt["stage_key"],
            "attempt": attempt["attempt"],
            "grant_id": attempt["grant_id"],
        }
        verdict = assess(connection, attempt, campaign, _provider(connection, attempt))
        if isinstance(verdict, Refused):
            entry.update(verdict="refused", refusal=verdict.reason, readable=verdict.readable)
            if verdict.provider_reason:
                entry["provider_reason"] = verdict.provider_reason
        else:
            entry.update(proof=verdict.proof, evidence_digest=verdict.digest)
            seen = requested(connection, campaign, attempt["id"], verdict.digest)
            entry["verdict"] = "requested" if seen else "reconcilable"
        entries.append(entry)
    return entries


class UnclaimedRecovery:
    """``CampaignStore`` mixin: list and request the unclaimed unwind of unknown attempts."""

    def _unclaimed_actor(self, connection: sqlite3.Connection, live: dict[str, Any]) -> None:
        """The owner's own credential, like a campaign cancel; never an agent or worker."""
        if (
            live.get("kind") == "worker"
            or self._external_scope(connection, live)
            or live["role"] != "human"
        ):
            raise Forbidden("Unclaimed attempt recovery needs the owner's control credential.")

    def list_unclaimed_attempts(self, principal, campaign_id: str) -> dict[str, Any]:
        """Dry run: every unknown attempt of the campaign with its verdict; changes nothing."""
        connection = _connect(self.database_path)
        try:
            live = self._actor(connection, principal)
            self._unclaimed_actor(connection, live)
            campaign = self._readable_campaign(connection, live, campaign_id)
            entries = assess_campaign(connection, campaign)
        finally:
            connection.close()
        return {
            "campaign_id": campaign_id,
            "campaign_status": campaign["status"],
            "dry_run": True,
            "attempts": entries,
        }

    def reconcile_unclaimed_attempts(
        self, principal, campaign_id: str, key: str, digest: str, payload
    ) -> tuple[int, dict[str, Any]]:
        try:
            body = ReconcileUnclaimedAttempts.model_validate(payload)
        except ValidationError as error:
            raise StoreError("Invalid unclaimed attempt recovery request.") from error
        if body.dry_run:
            return 200, self.list_unclaimed_attempts(principal, campaign_id)

        def command(connection: sqlite3.Connection, live: dict[str, Any]):
            self._unclaimed_actor(connection, live)
            campaign = dict(self._readable_campaign(connection, live, campaign_id))
            if campaign["status"] in TERMINAL_CAMPAIGN:
                raise Conflict("A finished campaign has no attempt for the runner to reconcile.")
            entries = assess_campaign(connection, campaign)
            accepted = [e for e in entries if e["verdict"] == "reconcilable"]
            if not accepted and not any(e["verdict"] == "requested" for e in entries):
                raise Conflict(
                    "No unknown attempt of this campaign provably claimed no provider create: "
                    + (
                        ", ".join(
                            f"{e['stage_key']}#{e['attempt']}:{e.get('refusal', '')}"
                            for e in entries
                        )
                        or "no unknown attempt"
                    )[:400]
                )
            for entry in accepted:
                self._event(
                    connection,
                    live,
                    campaign["project_id"],
                    None,
                    REQUEST_EVENT,
                    1,
                    {
                        "campaign_id": campaign["id"],
                        "attempt_id": entry["campaign_attempt_id"],
                        "stage_key": entry["stage_key"],
                        "attempt": entry["attempt"],
                        "grant_id": entry["grant_id"],
                        "proof": entry["proof"],
                        "evidence_digest": entry["evidence_digest"],
                        "reason": body.reason,
                        "requested_by_principal_id": live["id"],
                        "settlement_cents": 0,
                        "provider_termination_inferred": False,
                    },
                )
                entry["verdict"] = "requested"
            return 200, {
                "campaign_id": campaign["id"],
                "campaign_status": campaign["status"],
                "dry_run": False,
                "requested": [e["campaign_attempt_id"] for e in accepted],
                "attempts": entries,
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/campaigns/{campaign_id}/reconcile-unclaimed-attempts",
            digest,
            command,
        )
