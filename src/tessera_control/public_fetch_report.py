"""Record a local worker's public-fetch receipt as one append-only event (ADR 0097).

The files never reach the control plane. The worker supervisor downloads and verifies them on
its own host; only these rows (source host, digest, bytes, timing) are reported, and a row that
contradicts the frozen plan's pins is refused.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, worker_principal
from .egress_journal import LOCAL_CPU_ADAPTER
from .execution import ExecutionStore
from .migrations import plan_payload
from .public_fetch_models import PublicFetchPlan, ReportPublicFetch
from .store import Conflict, NotFound, canonical_digest

EVENT_TYPE = "execution.public_fetch_recorded"


def public_fetch_for_operation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type = ?
        AND json_extract(payload_json, '$.operation_id') = ? ORDER BY rowid LIMIT 1""",
        (EVENT_TYPE, operation_id),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def check_rows_against_plan(plan: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Every pinned file reported exactly once, with the pinned digest, host and size bound."""
    pins = {(f["input"], f["name"]): f for f in plan["files"]}
    seen = {(row["input"], row["name"]) for row in rows}
    if len(seen) != len(rows) or seen != set(pins):
        raise Conflict("Public fetch report does not cover exactly the planned files.")
    for row in rows:
        pin = pins[(row["input"], row["name"])]
        if (
            row["sha256"] != pin["sha256"]
            or row["bytes"] > pin["max_bytes"]
            or row["source_host"] != urlsplit(pin["url"]).hostname
        ):
            raise Conflict("Public fetch report contradicts the frozen plan.")


class PublicFetchStore(ExecutionStore):
    def _frozen_public_fetch(
        self, connection: sqlite3.Connection, worker: dict[str, Any], operation: sqlite3.Row
    ) -> dict[str, Any] | None:
        grant = self._grant(connection, worker, operation["grant_id"])
        revision = connection.execute(
            """SELECT * FROM task_revisions WHERE task_id = ? AND plan_digest = ?
            AND revision < ? ORDER BY revision DESC LIMIT 1""",
            (grant["task_id"], grant["plan_digest"], grant["task_revision"]),
        ).fetchone()
        if revision is None:
            raise Conflict("Approved execution plan is missing.")
        try:
            spec = json.loads(revision["execution_spec_json"])
            plan = plan_payload(
                revision["title"],
                revision["purpose"],
                revision["budget_cents"],
                revision["max_duration_seconds"],
                spec,
            )
        except (ValueError, TypeError) as error:
            raise Conflict("Approved execution plan is invalid.") from error
        if canonical_digest(plan) != grant["plan_digest"]:
            raise Conflict("Approved execution plan checksum is invalid.")
        if spec.get("public_fetch") is None:
            return None
        return PublicFetchPlan.model_validate(spec["public_fetch"]).model_dump(mode="json")

    def report_public_fetch(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReportPublicFetch, payload)
        path = f"/worker/operations/{operation_id}/public-fetch"

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if (
                operation is None
                or operation["worker_principal_id"] != worker["id"]
                or operation["org_id"] != worker["org_id"]
            ):
                raise NotFound("External operation was not found.")
            self._fence_recovered_operation(connection, operation_id)
            if operation["lease_id"] != payload["lease_id"]:
                raise Conflict("Public fetch report does not match the external operation.")
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
            ).fetchone()
            if (
                lease is None
                or lease["epoch"] != payload["expected_epoch"]
                or operation["epoch"] != lease["epoch"]
            ):
                raise Conflict("Public fetch report epoch is no longer current.")
            if operation["adapter_version"] != LOCAL_CPU_ADAPTER:
                raise Conflict("Public fetch reports are accepted only for local-cpu operations.")
            plan = self._frozen_public_fetch(connection, worker, operation)
            if plan is None:
                raise Conflict("Operation plan does not declare a public fetch.")
            check_rows_against_plan(plan, payload["files"])
            if public_fetch_for_operation(connection, operation_id) is not None:
                raise Conflict("A public fetch was already recorded for this operation.")
            event = {
                "operation_id": operation_id,
                # Optional keys are stored only when present, so a row without the cache
                # outcome keeps the exact shape it always had.
                "files": [
                    {k: v for k, v in row.items() if k not in ("cache", "cached") or v is not None}
                    for row in payload["files"]
                ],
                "hosts": plan["hosts"],
                "via_control": False,
            }
            self._event(
                connection,
                worker,
                operation["project_id"],
                operation["task_id"],
                EVENT_TYPE,
                operation["epoch"],
                event,
            )
            return 201, event | {"recorded": True}

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")


router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def public_fetch_store(request: Request) -> PublicFetchStore:
    return request.app.state.public_fetch


Store = Annotated[PublicFetchStore, Depends(public_fetch_store)]


@router.post("/worker/operations/{operation_id}/public-fetch")
def report_public_fetch(
    operation_id: str, body: ReportPublicFetch, worker: Worker, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json")
    status, response = store.report_public_fetch(
        worker, operation_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(response, status_code=status)
