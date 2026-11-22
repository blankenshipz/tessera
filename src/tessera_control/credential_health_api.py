"""``GET /credential-health``: expiry state of provider credentials, metadata only.

A human, or an agent with ``provider_trust:read``, may read it; a worker may not. Entries name a
credential by its reference and version and give a state and the seconds remaining. No value is
ever read here: the providers read only a metadata header.
"""

from __future__ import annotations

import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from . import credential_health
from .external_agent_api import Actor as ExternalActor
from .external_agents import ExternalAgentStore
from .store import Forbidden, _connect

router = APIRouter()


class CredentialHealthStore(ExternalAgentStore):
    def read(self, principal) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._actor(connection, principal)
            if actor["kind"] == "worker":
                raise Forbidden("Workers cannot read credential health.")
            if actor["role"] == "agent":
                self._scope(connection, actor, "provider_trust:read")
            elif actor["role"] != "human":
                raise Forbidden("A human or a provider_trust:read agent is required.")
        finally:
            connection.close()
        now = time.time()
        summary = credential_health.summarize(credential_health.collect(now))
        return {
            "checked_at": now,
            "warn_seconds": credential_health.WARN_SECONDS,
            "blocking_states": sorted(credential_health.BLOCKING_STATES),
            **summary,
        }


def credential_health_store(request: Request) -> CredentialHealthStore:
    state = request.app.state
    store = getattr(state, "credential_health", None)
    if store is None:
        store = CredentialHealthStore(state.admin.database_path.parent)
        state.credential_health = store
    return store


Health = Annotated[CredentialHealthStore, Depends(credential_health_store)]


@router.get("/credential-health")
def read_credential_health(actor: ExternalActor, store: Health):
    """Per credential: reference, version, expiry, state ('ok'|'warning'|'expired'|'revoked'|
    'version_mismatch'|'unknown') and seconds remaining. Never a value."""
    return store.read(actor)
