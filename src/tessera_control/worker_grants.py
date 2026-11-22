"""Read-only listing of the calling worker's not-yet-leased execution grants."""

from __future__ import annotations

from typing import Any

from .execution import ExecutionStore
from .store import _connect, utc_now

MAX_PENDING_GRANTS = 50


class WorkerGrantStore(ExecutionStore):
    def list_pending(self, worker: dict[str, Any]) -> dict[str, Any]:
        """Active, unexpired, never-leased grants for this worker credential only.

        A grant for a campaign stage task is listed only once its experiment attempt is
        admitted, so a worker can never start work the runner has not yet bound, and only while
        its campaign is running (cancel, pause and block stop new leases). A Salad stage grant
        is listed only while its attempt awaits a lease (ADR 0038 amendment).
        """
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, worker, audience="worker")
            if live["kind"] != "worker" or live["project_id"] is None:
                return {"grants": []}
            now = utc_now()
            rows = connection.execute(
                """SELECT g.id, g.task_id, g.pinned_image, g.expires_at, g.created_at
                FROM execution_grants g
                WHERE g.worker_principal_id = ? AND g.project_id = ?
                AND g.status = 'active' AND g.expires_at > ?
                AND NOT EXISTS (SELECT 1 FROM execution_leases l WHERE l.grant_id = g.id)
                AND (
                    NOT EXISTS (SELECT 1 FROM campaign_stages s WHERE s.task_id = g.task_id)
                    OR EXISTS (SELECT 1 FROM research_experiment_admitted_attempts a
                               WHERE a.grant_id = g.id)
                )
                AND NOT EXISTS (
                    SELECT 1 FROM campaign_stage_attempts ca JOIN campaigns c
                    ON c.id = ca.campaign_id WHERE ca.grant_id = g.id AND c.status <> 'running'
                )
                AND NOT EXISTS (
                    SELECT 1 FROM campaign_salad_attempts sa JOIN campaign_stage_attempts ca
                    ON ca.id = sa.attempt_id WHERE ca.grant_id = g.id
                    AND sa.phase <> 'awaiting_lease'
                )
                ORDER BY g.created_at, g.id LIMIT ?""",
                (live["id"], live["project_id"], now, MAX_PENDING_GRANTS),
            ).fetchall()
            return {"grants": [dict(row) for row in rows]}
        finally:
            connection.close()
