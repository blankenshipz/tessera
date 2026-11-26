"""Zero-settlement reconciliation of a Salad grant that provably claimed no provider effect.

A leased and packaged Salad operation cannot use the Kubernetes never-submitted
attestations, and an ordinary reconcile needs a confirmed-terminated observation. This
store closes the narrow case in between (ADR 0038 amendment): the grant was cancelled,
exactly one ``kubernetes-job-v1`` operation exists, control holds no ``salad_proxy.claim``
for a create or start of that operation, and no route of the operation still wants to run or
has a create or start effect, other than one the trusted server refused before its use-time
claim (ADR 0205). Control writes the claim before any call to the provider, so no claim means
no provider mutation can have happened. It never infers a stop.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from .execution import ExecutionStore, reconcile_grant_row
from .execution_models import ExecutionRequest, Revision
from .salad_precreate import sent_launch_effects
from .store import Conflict, StoreError, utc_now

ADAPTER = "kubernetes-job-v1"
PROOF = "no_create_or_start_claim"

# Why a grant is not provably unclaimed (closed values). ``create_claimed`` and
# ``route_effect`` are claims of a provider effect; the others are unknown claim states.
REFUSALS = {
    "operation_count": "Exactly one Salad operation is required for this grant.",
    "create_claimed": "A provider create or start was claimed; evidence is required.",
    "route_effect": "A route of this operation still has a run or effect.",
    "route_wants_run": "A route of this operation still has a run or effect.",
}
CLAIMS = frozenset({"create_claimed", "route_effect"})


def create_claimed(connection: sqlite3.Connection, org_id: str, operation_id: str | None) -> bool:
    """True when control holds a provider create or start claim for the operation.

    The one reading of a ``salad_proxy.claim`` for an operation: control writes the claim in
    the same transaction as the intent to call the provider (ADR 0038 amendment)."""
    if operation_id is None:
        return False
    row = connection.execute(
        """SELECT 1 FROM events WHERE org_id=? AND event_type='salad_proxy.claim'
        AND json_extract(payload_json,'$.operation_id')=?
        AND json_extract(payload_json,'$.action') IN ('create','start') LIMIT 1""",
        (org_id, operation_id),
    ).fetchone()
    return row is not None


def unclaimed_proof(
    connection: sqlite3.Connection, org_id: str, grant_id: str
) -> tuple[str | None, dict[str, Any]]:
    """``(refusal, proof)``: refusal is None when the grant provably claimed no provider effect.

    A pure read of the operation, claim and route records; it never looks at the grant's
    status, so each caller checks that itself. ``proof`` names the one operation and the
    routes whose newest state was read."""
    operations = connection.execute(
        "SELECT operation_id, adapter_version FROM external_operations WHERE grant_id=?",
        (grant_id,),
    ).fetchall()
    if len(operations) != 1 or operations[0]["adapter_version"] != ADAPTER:
        return "operation_count", {}
    operation_id = operations[0]["operation_id"]
    proof: dict[str, Any] = {"operation_id": operation_id, "route_ids": []}
    if create_claimed(connection, org_id, operation_id):
        return "create_claimed", proof
    seen: set[str] = set()
    for row in connection.execute(
        """SELECT payload_json FROM events WHERE org_id=?
        AND event_type='remote_execution.state'
        AND json_extract(payload_json,'$.proposal.operation_id')=?
        ORDER BY cursor DESC""",
        (org_id, operation_id),
    ):
        state = json.loads(row["payload_json"])
        if state["id"] in seen:
            continue  # newest state per route only
        seen.add(state["id"])
        if sent_launch_effects(state):
            # A create/start effect the trusted server refused before its use-time claim (and
            # no claim exists for the operation, checked above) was never sent (ADR 0205).
            return "route_effect", proof
        if state.get("desired") == "run":
            return "route_wants_run", proof
    proof["route_ids"] = sorted(seen)
    return None, proof


def unclaimed_event(grant_id: str, proof: dict[str, Any], currency: str) -> dict[str, Any]:
    """The ``execution.grant_reconciled_unclaimed`` payload, for a person or the runner."""
    return {
        "grant_id": grant_id,
        "operation_id": proof["operation_id"],
        "route_ids": proof["route_ids"],
        "proof": PROOF,
        "settlement_cents": 0,
        "currency": currency,
        "provider_termination_inferred": False,
        "operational_only": True,
    }


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
            refusal, proof = unclaimed_proof(connection, actor["org_id"], grant_id)
            if refusal is not None:
                raise Conflict(REFUSALS[refusal])
            limits = self._limit(connection, actor, grant["project_id"])
            now = utc_now()
            # The shared reconcile accounting (ADR 0048), at zero.
            if (
                reconcile_grant_row(
                    connection, grant, settlement_cents=0, principal_id=actor["id"], now=now
                )
                is None
            ):
                raise Conflict("Execution reservation ledger is inconsistent; authority held.")
            next_revision = grant["revision"] + 1
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
                unclaimed_event(grant_id, proof, limits["currency"]),
            )
            return 200, self._grant_response(connection, updated)

        return self._mutate(principal, key, "POST", path, digest, command)
