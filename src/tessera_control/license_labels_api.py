"""Human-set, add-only licence labels on project snapshots, and the read of them (ADR 0113).

A label is an operator assertion about what a snapshot's data may be used for (for example a
licence class). It is separate from the public/private data class (ADR 0039). Only a person
adds labels, a label is never removed, and a provider may be limited to data whose every label it
allows (the provider trust file's ``allowed_license_labels``). Data with no label carries the
implicit label ``unlabelled``. Reads are for a person or an agent with ``snapshot:read``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from . import license_labels
from .approval_trust import require_decision_actor
from .campaign_store import CampaignStore
from .execution_api import Actor, Key, result
from .external_agent_api import Actor as ExternalActor
from .snapshots import SnapshotStore
from .store import Forbidden, NotFound, StoreError, _connect, canonical_digest, utc_now

EVENT = "snapshot.license_labels_added"
SNAPSHOT_SCOPE = "snapshot:read"
Label = Annotated[str, StringConstraints(pattern=license_labels.LABEL_RE.pattern)]
router = APIRouter(prefix="/projects/{project_id}/snapshots/{snapshot_id}/license-labels")


class AddLabels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    add: Annotated[list[Label], Field(min_length=1, max_length=license_labels.MAX_LABELS)]


class LicenseLabelStore(CampaignStore):
    @staticmethod
    def _owned_snapshot(connection, actor, project_id: str, snapshot_id: str):
        """The snapshot inside the actor's organization and the named project, else 404."""
        row = SnapshotStore._snapshot(connection, actor, snapshot_id)
        if row["project_id"] != project_id:
            raise NotFound("Snapshot was not found.")
        return row

    def read(self, principal, project_id: str, snapshot_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._actor(connection, principal)
            self._require_action(connection, actor, SNAPSHOT_SCOPE)
            self._project(connection, actor, project_id)
            self._owned_snapshot(connection, actor, project_id, snapshot_id)
            labels = license_labels.snapshot_labels(connection, snapshot_id)
            return {
                "snapshot_id": snapshot_id,
                "labels": sorted(labels),
                "labelled": labels != frozenset({license_labels.UNLABELLED}),
            }
        finally:
            connection.close()

    def add(
        self, principal, project_id: str, snapshot_id: str, key: str, body: AddLabels
    ) -> tuple[int, dict]:
        payload = body.model_dump(mode="json")

        def command(connection, live):
            if live["role"] != "human":
                raise Forbidden("Only a person may label a snapshot.")
            require_decision_actor(live, forbidden=Forbidden)
            self._project(connection, live, project_id)
            self._owned_snapshot(connection, live, project_id, snapshot_id)
            try:
                outcome = license_labels.add_snapshot_labels(
                    connection, live, snapshot_id, frozenset(body.add), utc_now()
                )
            except ValueError as error:  # the per-snapshot limit: a clear 400, nothing written
                raise StoreError(str(error)) from None
            labels = sorted(outcome["labels"])
            added = sorted(outcome["added"])
            if added:
                self._event(
                    connection,
                    live,
                    project_id,
                    None,
                    EVENT,
                    outcome["revision"],
                    {
                        "snapshot_id": snapshot_id,
                        "added": added,
                        "labels": labels,
                        "revision": outcome["revision"],
                    },
                )
            return 200, {
                "snapshot_id": snapshot_id,
                "labels": labels,
                "added": added,
                "revision": outcome["revision"],
            }

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/snapshots/{snapshot_id}/license-labels",
            canonical_digest(payload),
            command,
        )


def label_store(request: Request) -> LicenseLabelStore:
    state = request.app.state
    store = getattr(state, "license_labels", None)
    if store is None:
        store = LicenseLabelStore(state.admin.database_path.parent)
        state.license_labels = store
    return store


Labels = Annotated[LicenseLabelStore, Depends(label_store)]


@router.get("")
def read_license_labels(project_id: str, snapshot_id: str, actor: ExternalActor, store: Labels):
    """The snapshot's labels (the implicit ``unlabelled`` when none); metadata only."""
    return store.read(actor, project_id, snapshot_id)


@router.post("")
def add_license_labels(
    project_id: str, snapshot_id: str, body: AddLabels, actor: Actor, key: Key, store: Labels
):
    """Human only, add-only and idempotent: record labels on a snapshot."""
    return result(store.add(actor, project_id, snapshot_id, key, body))
