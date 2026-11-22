"""Zero-settlement reconciliation of a Salad grant that provably claimed no provider effect.

A leased and packaged Salad operation cannot use the Kubernetes never-submitted
attestations, and an ordinary reconcile needs a confirmed-terminated observation. This
store closes the narrow case in between (ADR 0038 amendment): the grant was cancelled,
exactly one ``kubernetes-job-v1`` operation exists, control holds no ``salad_proxy.claim``
for a create or start of that operation, and no route of the operation still wants to run.
Control writes the claim in the same transaction as the intent to call the provider, so
no claim means no provider mutation can have happened. It never infers a stop.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from .execution import ExecutionStore
from .execution_models import ExecutionRequest, Revision
from .store import Conflict, StoreError, utc_now

ADAPTER = "kubernetes-job-v1"
PROOF = "no_create_or_start_claim"


class ReconcileUnclaimedSalad(ExecutionRequest):
    expected_revision: Revision


class SaladUnclaimedStore(ExecutionStore):
    def reconcile(
        self,
        principal: dict[str, Any],
        grant_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        try:
            payload = ReconcileUnclaimedSalad.model_validate(payload).model_dump(mode="json")
        except ValidationError:
            raise StoreError("Invalid unclaimed Salad reconcile request.") from None
        path = f"/execution-grants/{grant_id}/reconcile-unclaimed-salad"

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._require_human(actor)
            grant = self._grant(connection, actor, grant_id)
            if grant["revision"] != payload["expected_revision"]:
                raise Conflict("Execution grant revision is no longer current.")
            if grant["status"] != "cancel_requested":
                raise Conflict("Only a cancelled Salad grant can be reconciled as unclaimed.")
            operations = connection.execute(
                "SELECT operation_id, adapter_version FROM external_operations WHERE grant_id=?",
                (grant_id,),
            ).fetchall()
            if len(operations) != 1 or operations[0]["adapter_version"] != ADAPTER:
                raise Conflict("Exactly one Salad operation is required for this grant.")
            operation_id = operations[0]["operation_id"]
            claims = connection.execute(
                """SELECT count(*) FROM events WHERE org_id=? AND event_type='salad_proxy.claim'
                AND json_extract(payload_json,'$.operation_id')=?
                AND json_extract(payload_json,'$.action') IN ('create','start')""",
                (actor["org_id"], operation_id),
            ).fetchone()[0]
            if claims:
                raise Conflict("A provider create or start was claimed; evidence is required.")
            route_ids: list[str] = []
            seen: set[str] = set()
            for row in connection.execute(
                """SELECT payload_json FROM events WHERE org_id=?
                AND event_type='remote_execution.state'
                AND json_extract(payload_json,'$.proposal.operation_id')=?
                ORDER BY cursor DESC""",
                (actor["org_id"], operation_id),
            ):
                state = json.loads(row["payload_json"])
                if state["id"] in seen:
                    continue  # newest state per route only
                seen.add(state["id"])
                route_ids.append(state["id"])
                if state.get("desired") == "run" or any(
                    effect.get("action") in {"create", "start"} for effect in state["effects"]
                ):
                    raise Conflict("A route of this operation still has a run or effect.")
            limits = self._limit(connection, actor, grant["project_id"])
            now = utc_now()
            next_revision = grant["revision"] + 1
            changed = connection.execute(
                """UPDATE execution_grants SET status='reconciled', revision=?, updated_at=?,
                reconciled_at=?, reconciled_by_principal_id=?, settlement_cents=0
                WHERE id=? AND revision=? AND status='cancel_requested'""",
                (next_revision, now, now, actor["id"], grant_id, grant["revision"]),
            ).rowcount
            if changed != 1:
                raise Conflict("Execution authority is no longer unresolved.")
            released = connection.execute(
                """UPDATE execution_project_limits SET held_cents=held_cents-?,updated_at=?
                WHERE project_id=? AND held_cents>=?""",
                (
                    grant["max_exposure_cents"],
                    now,
                    grant["project_id"],
                    grant["max_exposure_cents"],
                ),
            ).rowcount
            if released != 1:
                raise Conflict("Execution reservation ledger is inconsistent; authority held.")
            updated = dict(grant)
            updated.update(
                status="reconciled",
                revision=next_revision,
                updated_at=now,
                reconciled_at=now,
                reconciled_by_principal_id=actor["id"],
                settlement_cents=0,
            )
            self._event(
                connection,
                actor,
                grant["project_id"],
                grant["task_id"],
                "execution.grant_reconciled_unclaimed",
                next_revision,
                {
                    "grant_id": grant_id,
                    "operation_id": operation_id,
                    "route_ids": sorted(route_ids),
                    "proof": PROOF,
                    "settlement_cents": 0,
                    "currency": limits["currency"],
                    "provider_termination_inferred": False,
                    "operational_only": True,
                },
            )
            return 200, self._grant_response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command)
