"""Control API for the stuck-campaign detector: scan, list, acknowledge, settings.

Reads and scans are for a human or an agent with ``campaign:read``; acknowledging needs a human
or an agent with ``campaign:cancel`` (the scope that already means "may steer a campaign");
settings are human only. Nothing here acts on a campaign: findings and their receipts are the
whole effect.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import campaign_stuck as stuck
from .approval_trust import require_decision_actor
from .execution_api import result
from .external_agent_api import Actor, Key
from .external_agents import ExternalAgentStore
from .store import Conflict, Forbidden, NotFound, _connect, canonical_digest, utc_now

router = APIRouter(prefix="/projects/{project_id}/campaign-stuck")
Note = Annotated[str, StringConstraints(max_length=500, pattern=r"^[^\x00-\x1f\x7f]*$")]


class AcknowledgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: Note | None = None


class SettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unchanged_seconds: Annotated[int, Field(strict=True, ge=60, le=30 * 86400)]
    lease_grace_seconds: Annotated[int, Field(strict=True, ge=0, le=86400)]


class CampaignStuckStore(ExternalAgentStore):
    def __init__(self, state_dir, *, clock=None):
        super().__init__(state_dir)
        self.clock = clock or (lambda: datetime.now(UTC))

    def _may(self, connection, actor, action: str | None) -> None:
        """A human, or an enrolled agent holding ``action``; never a worker."""
        if actor["kind"] == "worker":
            raise Forbidden("Workers cannot use the stuck-campaign findings.")
        if actor["role"] == "agent":
            self._scope(connection, actor, action)
        elif actor["role"] != "human":
            raise Forbidden("A human or an enrolled agent is required.")

    def read(self, principal, project_id: str, status: str, limit: int) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._actor(connection, principal)
            self._may(connection, actor, "campaign:read")
            self._project(connection, actor, project_id)
            return {
                "project_id": project_id,
                "settings": stuck.get_settings(connection, actor["org_id"], project_id),
                "findings": stuck.list_findings(
                    connection, actor["org_id"], project_id, status, limit
                ),
            }
        finally:
            connection.close()

    def scan(self, principal, project_id: str, key: str) -> tuple[int, dict[str, Any]]:
        def command(connection, live):
            self._may(connection, live, "campaign:read")
            self._project(connection, live, project_id)
            return 200, stuck.scan_project(
                connection, live["org_id"], project_id, self.clock(), lambda _c: live["id"]
            )

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/campaign-stuck/scan",
            canonical_digest({}),
            command,
        )

    def acknowledge(
        self, principal, project_id: str, finding_id: str, key: str, body: AcknowledgeBody
    ) -> tuple[int, dict[str, Any]]:
        def command(connection, live):
            self._may(connection, live, "campaign:cancel")
            self._project(connection, live, project_id)
            found = stuck.acknowledge(
                connection, live["org_id"], project_id, finding_id, live["id"], body.note,
                self.clock(),
            )  # fmt: skip
            if found is None:
                raise NotFound("Finding was not found.")
            if found["status"] == "resolved":
                raise Conflict("The finding is resolved; nothing to acknowledge.")
            return 200, found

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/campaign-stuck/{finding_id}/acknowledge",
            canonical_digest(body.model_dump(mode="json")),
            command,
        )

    def set_settings(
        self, principal, project_id: str, key: str, body: SettingsBody
    ) -> tuple[int, dict[str, Any]]:
        def command(connection, live):
            if live["role"] != "human":
                raise Forbidden("Only a human may change the stuck-campaign settings.")
            require_decision_actor(live, forbidden=Forbidden)
            self._project(connection, live, project_id)
            now = utc_now()
            connection.execute(
                """INSERT INTO campaign_stuck_settings (org_id, project_id, unchanged_seconds,
                lease_grace_seconds, revision, updated_by_principal_id, updated_at)
                VALUES (?,?,?,?,1,?,?) ON CONFLICT(org_id, project_id) DO UPDATE SET
                unchanged_seconds=excluded.unchanged_seconds,
                lease_grace_seconds=excluded.lease_grace_seconds,
                revision=campaign_stuck_settings.revision+1,
                updated_by_principal_id=excluded.updated_by_principal_id,
                updated_at=excluded.updated_at""",
                (live["org_id"], project_id, body.unchanged_seconds, body.lease_grace_seconds,
                 live["id"], now),
            )  # fmt: skip
            self._event(
                connection, live, project_id, None, stuck.SETTINGS_SET, 1,
                {"unchanged_seconds": body.unchanged_seconds,
                 "lease_grace_seconds": body.lease_grace_seconds},
            )  # fmt: skip
            return 200, stuck.get_settings(connection, live["org_id"], project_id)

        return self._mutate(
            principal,
            key,
            "PUT",
            f"/projects/{project_id}/campaign-stuck/settings",
            canonical_digest(body.model_dump(mode="json")),
            command,
        )


def stuck_store(request: Request) -> CampaignStuckStore:
    state = request.app.state
    store = getattr(state, "campaign_stuck", None)
    if store is None:
        store = CampaignStuckStore(state.admin.database_path.parent)
        state.campaign_stuck = store
    return store


Stuck = Annotated[CampaignStuckStore, Depends(stuck_store)]


@router.get("")
def list_stuck(
    project_id: str,
    actor: Actor,
    store: Stuck,
    status: Literal["open", "acknowledged", "resolved", "all"] = "open",
    limit: int = Query(default=50, ge=1, le=200),
):
    return store.read(actor, project_id, status, limit)


@router.post("/scan")
def scan_stuck(project_id: str, actor: Actor, key: Key, store: Stuck):
    return result(store.scan(actor, project_id, key))


@router.put("/settings")
def set_stuck_settings(project_id: str, body: SettingsBody, actor: Actor, key: Key, store: Stuck):
    return result(store.set_settings(actor, project_id, key, body))


@router.post("/{finding_id}/acknowledge")
def acknowledge_stuck(
    project_id: str, finding_id: str, body: AcknowledgeBody, actor: Actor, key: Key, store: Stuck
):
    return result(store.acknowledge(actor, project_id, finding_id, key, body))
