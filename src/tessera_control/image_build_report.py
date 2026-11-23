"""Record a local worker's image-build result as one append-only event (ADR 0123).

The image itself goes from the builder to the registry; only this row (image reference, digest,
size, whether it was pushed) is reported, and a row that contradicts the frozen build policy is
refused. A failed build is recorded by its closed code only.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from .api import idempotency_key, worker_principal
from .egress_journal import LOCAL_CPU_ADAPTER
from .execution import ExecutionStore
from .image_build_models import BuildPolicyModel, ReportImageBuild, check_row_against_policy
from .migrations import plan_payload
from .store import Conflict, NotFound, canonical_digest

EVENT_TYPE = "execution.image_built"
FAILED_EVENT_TYPE = "execution.image_build_failed"


def _recorded(
    connection: sqlite3.Connection, operation_id: str, event_type: str
) -> dict[str, Any] | None:
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type = ?
        AND json_extract(payload_json, '$.operation_id') = ? ORDER BY rowid LIMIT 1""",
        (event_type, operation_id),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def image_build_for_operation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    """The recorded built image of an operation (None when none, or when the build failed)."""
    return _recorded(connection, operation_id, EVENT_TYPE)


def image_build_failure_for_operation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    return _recorded(connection, operation_id, FAILED_EVENT_TYPE)


class ImageBuildStore(ExecutionStore):
    def _frozen_build(
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
        if spec.get("build") is None:
            return None
        return BuildPolicyModel.model_validate(spec["build"]).model_dump(mode="json")

    def report_image_build(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReportImageBuild, payload)
        path = f"/worker/operations/{operation_id}/image-build"

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
                raise Conflict("Image build report does not match the external operation.")
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
            ).fetchone()
            if (
                lease is None
                or lease["epoch"] != payload["expected_epoch"]
                or operation["epoch"] != lease["epoch"]
            ):
                raise Conflict("Image build report epoch is no longer current.")
            if operation["adapter_version"] != LOCAL_CPU_ADAPTER:
                raise Conflict("Image build reports are accepted only for local-cpu operations.")
            policy = self._frozen_build(connection, worker, operation)
            if policy is None:
                raise Conflict("Operation plan does not declare an image build.")
            if (
                _recorded(connection, operation_id, EVENT_TYPE) is not None
                or _recorded(connection, operation_id, FAILED_EVENT_TYPE) is not None
            ):
                raise Conflict("An image build was already recorded for this operation.")
            row = payload.get("result")
            if row is None:
                event_type = FAILED_EVENT_TYPE
                event = {
                    "operation_id": operation_id,
                    "failed": payload["failed"],
                    "via_control": False,
                }
            else:
                if not check_row_against_policy(policy, row):
                    raise Conflict("Image build report contradicts the frozen plan.")
                event_type = EVENT_TYPE
                event = {
                    "operation_id": operation_id,
                    "image": row["image"],
                    "digest": row["digest"],
                    "size_bytes": row["size_bytes"],
                    "pushed": row["pushed"],
                    "base_images": row["base_images"],
                    "via_control": False,
                }
                if row.get("seconds") is not None:
                    event["seconds"] = row["seconds"]
            self._event(
                connection,
                worker,
                operation["project_id"],
                operation["task_id"],
                event_type,
                operation["epoch"],
                event,
            )
            return 201, event | {"recorded": True}

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")


router = APIRouter()
Worker = Annotated[dict[str, Any], Depends(worker_principal)]
Key = Annotated[str, Depends(idempotency_key)]


def image_build_store(request: Request) -> ImageBuildStore:
    return request.app.state.image_build


Store = Annotated[ImageBuildStore, Depends(image_build_store)]


@router.post("/worker/operations/{operation_id}/image-build")
def report_image_build(
    operation_id: str, body: ReportImageBuild, worker: Worker, key: Key, store: Store
) -> JSONResponse:
    payload = body.model_dump(mode="json", exclude_none=True)
    status, response = store.report_image_build(
        worker, operation_id, key, canonical_digest(payload), payload
    )
    return JSONResponse(response, status_code=status)
