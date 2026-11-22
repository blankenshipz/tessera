"""Scoped, read-only campaign evidence for enrolled external agents.

Lets an agent that submits campaigns discover the snapshots it may cite, then read stage
outputs (metadata only), attempt diagnostics and campaign budget without a human relaying
them.  Every read needs its own enrolled action, stays inside the credential's project and
organization, selects explicit column allowlists, and grants no approval, spend, release or
staging authority.  Staging stays a human step; artifact bytes stay behind the release path.

Actions: ``snapshot:read`` (project snapshot catalog), ``artifact:read`` (stage output
metadata), ``campaign:logs`` (attempt diagnostics; Tessera keeps no raw stdout/stderr, so
this is recorded status, exit code, failure class and bounded detail), ``budget:read``
(campaign cap, holds and settlements).  Humans (unscoped, tenant-checked) read the same.
"""

import json
from typing import Any

from .campaign_holdout import campaign_claims, project_claims
from .campaign_infra_retry import infra_attempts
from .campaign_models import TERMINAL_CAMPAIGN, normalize, parse_spec_json
from .campaign_rerun import custody_released
from .campaign_store import CampaignStore
from .data_class import snapshot_data_class
from .store import StoreError, _connect

SNAPSHOT_SCOPE = "snapshot:read"
ARTIFACT_SCOPE = "artifact:read"
LOGS_SCOPE = "campaign:logs"
BUDGET_SCOPE = "budget:read"

BOUNDARY = (
    "Recorded Tessera records only. Absent records are not evidence of absence and nothing "
    "here is approval, release or evidence of scientific validity."
)
_ROW_CAP = 1000


class AgentCampaignReads(CampaignStore):
    def _reader(self, connection, principal, action):
        actor = self._actor(connection, principal)
        self._require_action(connection, actor, action)
        return actor

    def list_snapshots(self, principal, project_id, offset=0, limit=50) -> dict[str, Any]:
        if not 0 <= offset <= 2**63 - 1 or not 1 <= limit <= 200:
            raise StoreError("Invalid page bounds.")
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, SNAPSHOT_SCOPE)
            self._project(connection, actor, project_id)
            rows = connection.execute(
                """SELECT s.id, s.digest, s.created_at,
                json_extract(s.manifest_json, '$.total_bytes') AS total_bytes,
                json_array_length(s.manifest_json, '$.entries') AS file_count
                FROM source_snapshots s JOIN projects p ON p.id = s.project_id
                WHERE s.project_id = ? AND p.org_id = ?
                ORDER BY s.created_at, s.id LIMIT ? OFFSET ?""",
                (project_id, actor["org_id"], limit, offset),
            ).fetchall()
            snapshots = []
            for row in rows:
                item = dict(row)
                item["data_class"] = snapshot_data_class(connection, item["id"])
                snapshots.append(item)
            return {"snapshots": snapshots, "boundary": BOUNDARY}
        finally:
            connection.close()

    @staticmethod
    def _custody_released(connection, manifest_id) -> bool:
        """The condition a later campaign's artifact pin needs (campaign_io.resolve_artifact)."""
        return custody_released(connection, manifest_id)

    def campaign_outputs(self, principal, campaign_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, ARTIFACT_SCOPE)
            self._campaign(connection, actor, campaign_id)
            rows = connection.execute(
                """SELECT stage_key, attempt_id, name, kind, media_type, sha256, size_bytes,
                snapshot_id, manifest_id, data_class, created_at FROM campaign_stage_io
                WHERE campaign_id = ? AND direction = 'output'
                ORDER BY created_at, stage_key, name LIMIT ?""",
                (campaign_id, _ROW_CAP),
            ).fetchall()
            outputs = []
            for row in rows:
                item = dict(row)
                item["custody_released"] = self._custody_released(connection, row["manifest_id"])
                outputs.append(item)
            return {
                "campaign_id": campaign_id,
                "outputs": outputs,
                "truncated": len(rows) == _ROW_CAP,
                "boundary": BOUNDARY + " Metadata only; bytes need a human-released snapshot.",
            }
        finally:
            connection.close()

    def campaign_logs(self, principal, campaign_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, LOGS_SCOPE)
            campaign = self._campaign(connection, actor, campaign_id)
            stages = connection.execute(
                """SELECT stage_key, status, attempts_used, provider_id, updated_at
                FROM campaign_stages WHERE campaign_id = ? ORDER BY topo_index""",
                (campaign_id,),
            ).fetchall()
            attempts = connection.execute(
                """SELECT stage_key, attempt, status, exit_code, failure_class, failure_detail,
                provider_id, created_at, updated_at FROM campaign_stage_attempts
                WHERE campaign_id = ? ORDER BY stage_key, attempt LIMIT ?""",
                (campaign_id, _ROW_CAP),
            ).fetchall()
            return {
                "campaign_id": campaign_id,
                "status": campaign["status"],
                "stages": [dict(row) for row in stages],
                "attempts": [dict(row) for row in attempts],
                "truncated": len(attempts) == _ROW_CAP,
                "boundary": BOUNDARY + " Raw stdout/stderr is not retained by Tessera.",
            }
        finally:
            connection.close()

    def campaign_grants(self, principal, campaign_id) -> dict[str, Any]:
        """Why a slot or release may still be held: each attempt's grant and data release.

        Recorded status only, from explicit column allowlists (campaign:logs). Reconciling a
        grant is human-only evidence work (ADR 0053 resolves a confirmed exit on its own);
        nothing here changes a grant.
        """
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, LOGS_SCOPE)
            self._campaign(connection, actor, campaign_id)
            rows = connection.execute(
                """SELECT a.stage_key, a.attempt, a.status AS attempt_status, a.failure_class,
                a.hold_cents, a.settled_cents, a.settlement_basis,
                g.id AS grant_id, g.status AS grant_status, g.max_exposure_cents,
                g.expires_at AS grant_expires_at, g.settlement_cents AS grant_settlement_cents,
                g.revision AS grant_revision,
                r.id AS release_id, r.status AS release_status, r.revision AS release_revision,
                r.expires_at AS release_expires_at
                FROM campaign_stage_attempts a
                LEFT JOIN execution_grants g ON g.id = a.grant_id
                LEFT JOIN snapshot_releases r ON r.id = a.release_id
                WHERE a.campaign_id = ? ORDER BY a.stage_key, a.attempt LIMIT ?""",
                (campaign_id, _ROW_CAP),
            ).fetchall()
            return {
                "campaign_id": campaign_id,
                "grants": [dict(row) for row in rows],
                "truncated": len(rows) == _ROW_CAP,
                "boundary": BOUNDARY + " An unresolved grant keeps holding its worker slot.",
            }
        finally:
            connection.close()

    def standing_status(self, principal, project_id) -> dict[str, Any]:
        """Why a scheduled or follow-on start did or did not happen (campaign:read).

        Standing approvals are created, revoked and shown in full only to a human. An agent
        sees recorded schedule status and the starts and pending triggers, from explicit
        column allowlists, never the template spec or any principal or credential id.
        """
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, "campaign:read")
            self._project(connection, actor, project_id)
            approvals = connection.execute(
                """SELECT id, status, "trigger", not_before, not_after, min_interval_seconds,
                max_starts, max_concurrent, per_start_budget_cap_cents, max_total_budget_cents,
                retry_max, retry_on, follow_on_standing_approval_id, spec_digest, created_at
                FROM campaign_standing_approvals WHERE project_id=? AND org_id=?
                ORDER BY created_at, id LIMIT ?""",
                (project_id, actor["org_id"], _ROW_CAP),
            ).fetchall()
            items = []
            for row in approvals:
                item = dict(row)
                item["starts"] = [
                    dict(r)
                    for r in connection.execute(
                        """SELECT start_index, campaign_id, kind, trigger_campaign_id,
                        retry_number, budget_cap_cents, created_at
                        FROM campaign_standing_starts WHERE standing_approval_id=?
                        ORDER BY start_index""",
                        (row["id"],),
                    )
                ]
                item["triggers"] = [
                    dict(r)
                    for r in connection.execute(
                        """SELECT trigger_campaign_id, kind, retry_number, status, start_index,
                        reason, created_at, resolved_at FROM campaign_standing_triggers
                        WHERE target_standing_approval_id=? ORDER BY created_at""",
                        (row["id"],),
                    )
                ]
                items.append(item)
            return {
                "project_id": project_id,
                "standing_approvals": items,
                "truncated": len(approvals) == _ROW_CAP,
                "boundary": BOUNDARY + " Agents cannot create, edit or revoke standing approvals.",
            }
        finally:
            connection.close()

    def campaign_retries(self, principal, campaign_id) -> dict[str, Any]:
        """Infrastructure-retry state per stage (campaign:read, ADR 0058).

        The policy the approved spec set, the attempts that ended for an infrastructure
        reason, what remains, and where the next attempt would run. Stopping the retries is
        stopping the campaign (``tessera_cancel_campaign``, campaign:cancel): nothing here
        changes state."""
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, "campaign:read")
            campaign = self._campaign(connection, actor, campaign_id)
            spec = parse_spec_json(campaign["spec_json"])
            normal = normalize(spec)
            stages = []
            for row in connection.execute(
                """SELECT stage_key, status, attempts_used FROM campaign_stages
                WHERE campaign_id = ? ORDER BY topo_index""",
                (campaign_id,),
            ):
                policy = normal.stage(row["stage_key"])
                attempts = [
                    dict(a)
                    for a in connection.execute(
                        """SELECT attempt, status, failure_class, failure_detail, provider_id,
                        exit_code, hold_cents, settled_cents FROM campaign_stage_attempts
                        WHERE campaign_id = ? AND stage_key = ? ORDER BY attempt""",
                        (campaign_id, row["stage_key"]),
                    )
                ]
                used = infra_attempts(attempts)
                limit = policy.retry.infra_retries
                stages.append(
                    {
                        "stage_key": row["stage_key"],
                        "stage_status": row["status"],
                        "infra_retries_max": limit,
                        "infra_retries_used": used,
                        "infra_retries_remaining": max(limit - used, 0),
                        "infra_exit_codes": list(policy.retry.infra_exit_codes),
                        "reroute_providers": list(policy.retry.reroute_providers),
                        "provider": policy.provider,
                        "infra_failures": [
                            a
                            for a in attempts
                            if str(a["failure_class"] or "").startswith("infra_")
                        ],
                        "attempts": len(attempts),
                    }
                )
            return {
                "campaign_id": campaign_id,
                "status": campaign["status"],
                "stages": stages,
                "stop": "tessera_cancel_campaign stops the campaign and so every retry",
                "boundary": BOUNDARY,
            }
        finally:
            connection.close()

    def campaign_holdout(self, principal, campaign_id) -> dict[str, Any]:
        """One-time holdout claims of a campaign and their scored metrics (campaign:read,
        ADR 0068). Per final evaluation stage the approved declaration, and the claim the
        platform recorded (absent until the runner opened it)."""
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, "campaign:read")
            campaign = self._campaign(connection, actor, campaign_id)
            listed = campaign_claims(connection, campaign_id, _ROW_CAP)
            claims = {c["stage_key"]: c for c in listed}
            stages = []
            for stage in normalize(parse_spec_json(campaign["spec_json"])).stages:
                if stage.evaluation is None:
                    continue
                declared = stage.holdout
                stages.append(
                    {
                        "stage_key": stage.key,
                        "final": stage.evaluation.final,
                        "metrics": [metric.name for metric in stage.metrics],
                        "holdout": None if declared is None else declared.model_dump(mode="json"),
                        "claim": claims.get(stage.key),
                    }
                )
            return {
                "campaign_id": campaign_id,
                "status": campaign["status"],
                "evaluation_stages": stages[:_ROW_CAP],
                "truncated": len(listed) == _ROW_CAP or len(stages) > _ROW_CAP,
                "boundary": BOUNDARY
                + " A scored claim records measured numbers, not a scientific verdict.",
            }
        finally:
            connection.close()

    def holdout_claims(self, principal, project_id, partition_digest=None) -> dict[str, Any]:
        """Every holdout claim in the project, optionally for one partition (campaign:read).

        A partition listed here is sealed for that scope: a new claim on it is refused."""
        if partition_digest is not None and (
            len(partition_digest) != 64
            or any(ch not in "0123456789abcdef" for ch in partition_digest)
        ):
            raise StoreError("partition_digest must be a lowercase sha256 hex digest.")
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, "campaign:read")
            self._project(connection, actor, project_id)
            claims = project_claims(
                connection, actor["org_id"], project_id, partition_digest, _ROW_CAP
            )
            return {
                "project_id": project_id,
                "claims": claims,
                "truncated": len(claims) == _ROW_CAP,
                "boundary": BOUNDARY,
            }
        finally:
            connection.close()

    def project_spend(self, principal, project_id) -> dict[str, Any]:
        """The operator's project limits and what campaigns hold and have settled (budget:read).

        Shows the ceiling a campaign budget or concurrency reduction works within; it changes
        nothing, and raising a limit stays with the operator. Aggregates only: no spec or
        provider detail.
        """
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, BUDGET_SCOPE)
            self._project(connection, actor, project_id)
            limits = connection.execute(
                """SELECT currency, monetary_limit_cents, max_concurrency, held_cents,
                settled_cents FROM execution_project_limits WHERE project_id=?""",
                (project_id,),
            ).fetchone()
            rows = connection.execute(
                """SELECT status, COUNT(*) AS campaigns, SUM(budget_cap_cents) AS cap_cents,
                SUM(held_cents) AS held_cents, SUM(settled_cents) AS settled_cents
                FROM campaigns WHERE project_id=? AND org_id=? GROUP BY status ORDER BY status""",
                (project_id, actor["org_id"]),
            ).fetchall()
            by_status = [dict(row) for row in rows]
            unfinished = [row for row in by_status if row["status"] not in TERMINAL_CAMPAIGN]
            result: dict[str, Any] = {
                "project_id": project_id,
                "limits": None,
                "campaigns_by_status": by_status,
                "unfinished": {
                    key: sum(row[key] for row in unfinished)
                    for key in ("campaigns", "cap_cents", "held_cents", "settled_cents")
                },
                "boundary": BOUNDARY
                + " Limits are the operator's; no tool raises them. Ledger records are not "
                "provider billing.",
            }
            if limits is not None:
                limit = dict(limits)
                limit["remaining_cents"] = (
                    limit["monetary_limit_cents"] - limit["held_cents"] - limit["settled_cents"]
                )
                result["limits"] = limit
            return result
        finally:
            connection.close()

    def campaign_budget(self, principal, campaign_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, BUDGET_SCOPE)
            campaign = self._campaign(connection, actor, campaign_id)
            spec = json.loads(campaign["spec_json"])
            cap, held, settled = (
                campaign["budget_cap_cents"],
                campaign["held_cents"],
                campaign["settled_cents"],
            )
            return {
                "campaign_id": campaign_id,
                "budget_cap_cents": cap,
                "held_cents": held,
                "settled_cents": settled,
                "remaining_cents": cap - held - settled,
                "currency": spec.get("currency", "USD"),
                "boundary": BOUNDARY,
            }
        finally:
            connection.close()
