"""Recorded requests to pause the operator's standing agent loop (ADR 0089).

The loop's local kill switch is a marker file the operator's machine reads. This store adds the
platform record: one ``agent_loop.paused`` event, written in the same transaction as the
idempotency record. It changes nothing else (no loop state, campaign, job or gate) and it is a
request record only; it does not prove the loop saw the marker and is not a model-call receipt.

Authorization: recording a pause and listing recorded pauses both accept a human or an agent
credential enrolled with ``loop:pause`` in the project. Every other credential (workers,
agents without the action, agents of another project) is refused, and the project is looked up
inside the caller's tenant, so a foreign project reads as not found.
"""

import hashlib
import json

from .agent_research import AgentResearchStore
from .models import NonBlank200, StrictRequest
from .store import Forbidden, _connect, canonical_digest

EVENT_TYPE = "agent_loop.paused"
SCOPE = "local_kill_switch_request"
BOUNDARY = (
    "Records that a pause was requested. It does not prove the operator's loop saw the marker "
    "and is not a model-call receipt."
)
LIST_CAP = 200


class PauseAgentLoop(StrictRequest):
    reason: NonBlank200


class AgentLoopControlStore(AgentResearchStore):
    def _authorize(self, connection, actor, project_id):
        if actor["role"] == "agent":
            self._scope(connection, actor, "loop:pause")
        elif actor["role"] != "human":
            raise Forbidden("Only a human or an enrolled agent may control the agent loop.")
        self._project(connection, actor, project_id)

    @staticmethod
    def _record(cursor, recorded_at, principal_id, extra=None):
        return {
            "event": EVENT_TYPE,
            "cursor": cursor,
            "recorded_at": recorded_at,
            "principal_id": principal_id,
            "boundary": BOUNDARY,
            **(extra or {}),
        }

    def pause(self, principal, project_id, key, payload):
        body = self._body(PauseAgentLoop, payload)
        reason = body["reason"]

        def command(connection, actor):
            self._authorize(connection, actor, project_id)
            self._event(
                connection,
                actor,
                project_id,
                None,
                EVENT_TYPE,
                1,
                {
                    "project_id": project_id,
                    "principal_id": actor["id"],
                    "credential_id": actor["credential_id"],
                    "credential_kind": actor.get("kind"),
                    "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
                    "reason": reason,
                    "scope": SCOPE,
                },
            )
            row = connection.execute(
                "SELECT cursor, created_at FROM events WHERE cursor=last_insert_rowid()"
            ).fetchone()
            return 201, self._record(row["cursor"], row["created_at"], actor["id"])

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/agent-loop/pause",
            canonical_digest(body),
            command,
            replay_guard=lambda c, a, r: self._authorize(c, a, project_id),
        )

    def list_pauses(self, principal, project_id, offset=0, limit=100):
        connection = _connect(self.database_path)
        try:
            connection.execute("BEGIN")
            actor = self._actor(connection, principal)
            self._authorize(connection, actor, project_id)
            rows = connection.execute(
                "SELECT cursor, created_at, actor_principal_id, payload_json FROM events "
                "WHERE org_id=? AND project_id=? AND event_type=? "
                "ORDER BY cursor DESC LIMIT ? OFFSET ?",
                (actor["org_id"], project_id, EVENT_TYPE, min(limit, LIST_CAP), offset),
            ).fetchall()
            return [
                self._record(
                    row["cursor"],
                    row["created_at"],
                    row["actor_principal_id"],
                    {
                        k: v
                        for k, v in json.loads(row["payload_json"]).items()
                        if k != "principal_id"
                    },
                )
                for row in rows
            ]
        finally:
            connection.close()
