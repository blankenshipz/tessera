"""Observed-provider run receipts, kept disjoint from fake-provider receipts.

A run receipt attributes one terminal observation (exit code, command digest,
wall-clock window, output digest) of a real provider to one immutable external
operation.  It is evidence that a process ran and exited; it never establishes
provider success, artifact release or scientific validity.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, StringConstraints, field_validator

from .artifacts import ArtifactStore
from .code_approval import grant_input_classes
from .data_class import OUTPUT_DATA_CLASS
from .execution import MAX_OBSERVATION_FUTURE_SKEW, UNRESOLVED_GRANT_STATUSES, ExecutionStore
from .execution_models import ExecutionRequest, Identifier, Revision
from .store import Conflict, NotFound, StoreError, _connect, _parse_utc, _utc_string, utc_now

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Int32 = Annotated[int, Field(strict=True, ge=-(2**31), le=2**31 - 1)]

RECEIPT_EVIDENCE = "observed_provider_execution"

# Server-side adapter -> provider kind mapping. Only the local Docker CPU adapter
# is mapped; remote (Kubernetes/Salad) mappings are deliberately left to the
# live-owned remote code so no remote run is attributed here by assumption.
ADAPTER_PROVIDER_KINDS = {"docker-local-cpu-v1": "local_docker_cpu"}

_IDENTITY_FIELDS = (
    "provider_id",
    "command_digest",
    "exit_code",
    "started_at",
    "finished_at",
    "output_digest",
    "receipt_digest",
)


MISMATCH = "Run receipt does not match control's recorded operation."


def argv_digest(argv: list[str]) -> str:
    """sha256 of the canonical JSON argv array.

    Duplicated (not imported) from ``tessera_worker.run_receipt.command_digest``;
    tests pin both implementations to the same vectors.
    """
    encoded = json.dumps(argv, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _manifests_for_operation(
    connection: sqlite3.Connection, operation_id: str, org_id: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """SELECT m.* FROM artifact_manifests m JOIN projects p ON p.id = m.project_id
        WHERE m.operation_id=? AND p.org_id=?""",
        (operation_id, org_id),
    ).fetchall()


def _verify_against_control_records(
    connection: sqlite3.Connection,
    operation: sqlite3.Row,
    grant: sqlite3.Row,
    lease: sqlite3.Row,
    operation_id: str,
    payload: dict[str, Any],
) -> None:
    """Fail closed unless the receipt matches what control itself recorded.

    Nothing worker-supplied is trusted: the plan digest, argv and manifest digest
    all come from control's grant, approved task revision and quarantined manifest.
    """
    body = payload["receipt_payload"]
    if (
        not isinstance(body.get("operation_id"), str)
        or not isinstance(body.get("plan_digest"), str)
        or body["operation_id"] != operation_id
        or body["plan_digest"] != grant["plan_digest"]
    ):
        raise Conflict(MISMATCH)
    try:
        plan, _ = ArtifactStore._plan(connection, grant)
        argv = plan["execution_spec"]["argv"]
        if not isinstance(argv, list) or not argv or any(not isinstance(a, str) for a in argv):
            raise ValueError
        expected_command = argv_digest(argv)
    except (Conflict, KeyError, TypeError, ValueError) as error:
        raise Conflict(MISMATCH) from error
    if payload["command_digest"] != expected_command:
        raise Conflict(MISMATCH)
    manifests = _manifests_for_operation(connection, operation_id, operation["org_id"])
    if len(manifests) != 1:
        raise Conflict(MISMATCH)
    manifest = manifests[0]
    if (
        manifest["project_id"] != operation["project_id"]
        or manifest["task_id"] != operation["task_id"]
        or manifest["grant_id"] != operation["grant_id"]
        or manifest["lease_id"] != operation["lease_id"]
        or manifest["epoch"] != operation["epoch"]
        or manifest["epoch"] != lease["epoch"]
        or manifest["worker_principal_id"] != operation["worker_principal_id"]
        or manifest["plan_digest"] != grant["plan_digest"]
        or manifest["manifest_digest"] != payload["output_digest"]
    ):
        raise Conflict(MISMATCH)


class ReportRunReceipt(ExecutionRequest):
    """Client-supplied facts only; provider kind and adapter come from the operation."""

    lease_id: Identifier
    expected_epoch: Revision
    provider_id: Identifier
    command_digest: Digest
    exit_code: Int32
    started_at: datetime
    finished_at: datetime
    output_digest: Digest
    receipt_payload: dict[str, Any]
    receipt_digest: Digest

    @field_validator("started_at", "finished_at", mode="before")
    @classmethod
    def timestamps_have_offset(cls, value: object) -> datetime:
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as error:
                raise ValueError("timestamp requires ISO-8601 time") from error
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("timestamp requires an ISO-8601 offset")
        return value


def _public(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    value = dict(row)
    value.pop("receipt_payload_json", None)
    value["receipt_payload"] = json.loads(row["receipt_payload_json"])
    value["receipt_evidence"] = RECEIPT_EVIDENCE
    value["provider_success"] = "not_established"
    value["scientific_validity"] = "not_assessed"
    value["operational_only"] = True
    return value


def _with_data_classes(connection: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
    """ADR 0039: the class of the grant input's data files and (amendment 1) its approved
    private code; run outputs are always private_derived."""
    classes = grant_input_classes(connection, value["grant_id"])
    return (
        value
        | classes
        | _trust_fields(connection, value)
        | {"output_data_class": OUTPUT_DATA_CLASS}
    )


_TRUST_KEYS = ("trust_provider_id", "trust_policy_digest", "admission_basis")


NOT_RECORDED = "not_recorded"


def _lease_trust_fields(
    connection: sqlite3.Connection, org_id: str, lease_id: str | None
) -> dict[str, Any]:
    """The trust fields recorded when the lease was acquired (ADR 0040 section 4); a lease
    that predates the record yields ``not_recorded``, never a value computed now."""
    row = connection.execute(
        """SELECT payload_json FROM events
        WHERE org_id = ? AND event_type = 'execution.lease_acquired'
        AND json_extract(payload_json, '$.lease_id') = ? ORDER BY cursor DESC LIMIT 1""",
        (org_id, lease_id),
    ).fetchone()
    recorded = {} if row is None else json.loads(row[0])
    if all(key in recorded for key in _TRUST_KEYS):
        return {key: recorded[key] for key in _TRUST_KEYS}
    return dict.fromkeys(_TRUST_KEYS, NOT_RECORDED)


def _trust_fields(connection: sqlite3.Connection, value: dict[str, Any]) -> dict[str, Any]:
    """ADR 0040 section 4: the provider instance, setting digest and admission basis as
    recorded with the receipt event; ``not_recorded`` for a receipt that predates them."""
    row = connection.execute(
        """SELECT payload_json FROM events
        WHERE org_id = ? AND event_type = 'worker.execution_run_receipt_recorded'
        AND json_extract(payload_json, '$.operation_id') = ? ORDER BY cursor DESC LIMIT 1""",
        (value["org_id"], value["operation_id"]),
    ).fetchone()
    if row is not None:
        recorded = json.loads(row[0])
        if all(key in recorded for key in _TRUST_KEYS):
            return {key: recorded[key] for key in _TRUST_KEYS}
    return dict.fromkeys(_TRUST_KEYS, NOT_RECORDED)


class ExecutionRunReceiptStore(ExecutionStore):
    def report_run_receipt(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReportRunReceipt, payload)
        path = f"/external-operations/{operation_id}/run-receipt"
        canonical = json.dumps(
            payload["receipt_payload"], sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        if len(canonical.encode()) > 65536:
            raise StoreError("Run receipt payload exceeds its bound.")
        if hashlib.sha256(canonical.encode()).hexdigest() != payload["receipt_digest"]:
            raise Conflict("Run receipt digest does not match its canonical payload.")
        started = _parse_utc(payload["started_at"])
        finished = _parse_utc(payload["finished_at"])
        if finished < started:
            raise Conflict("Run receipt finished before it started.")
        duration_ms = int((finished - started).total_seconds() * 1000)

        def command(
            connection: sqlite3.Connection, worker: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if operation is None or operation["worker_principal_id"] != worker["id"]:
                raise NotFound("External operation was not found.")
            self._fence_recovered_operation(connection, operation_id)
            if (
                operation["org_id"] != worker["org_id"]
                or operation["lease_id"] != payload["lease_id"]
            ):
                raise Conflict("Run receipt does not match the external operation.")
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
            ).fetchone()
            if lease is None or lease["epoch"] != payload["expected_epoch"]:
                raise Conflict("Run receipt epoch is no longer current.")
            grant = self._grant(connection, worker, operation["grant_id"])
            if (
                grant["project_id"] != operation["project_id"]
                or grant["task_id"] != operation["task_id"]
                or grant["worker_principal_id"] != worker["id"]
                or operation["epoch"] != lease["epoch"]
                or grant["status"] not in UNRESOLVED_GRANT_STATUSES
            ):
                raise Conflict("Run receipt authority is no longer current.")
            provider_kind = ADAPTER_PROVIDER_KINDS.get(operation["adapter_version"])
            if provider_kind is None:
                raise Conflict("No observed-provider receipt mapping for this adapter.")
            if started < _parse_utc(lease["issued_at"]) or (
                finished > self._now() + MAX_OBSERVATION_FUTURE_SKEW
            ):
                raise Conflict("Run receipt window is outside the lease window.")
            if connection.execute(
                "SELECT 1 FROM external_operation_receipts WHERE operation_id=?", (operation_id,)
            ).fetchone():
                raise Conflict("Operation already has fake provider evidence.")
            if operation["provider_id"] not in {None, payload["provider_id"]}:
                raise Conflict("Provider identity cannot change within an external operation.")
            _verify_against_control_records(
                connection, operation, grant, lease, operation_id, payload
            )
            identity = {
                "provider_id": payload["provider_id"],
                "command_digest": payload["command_digest"],
                "exit_code": payload["exit_code"],
                "started_at": _utc_string(started),
                "finished_at": _utc_string(finished),
                "output_digest": payload["output_digest"],
                "receipt_digest": payload["receipt_digest"],
            }
            existing = connection.execute(
                "SELECT * FROM execution_run_receipts WHERE operation_id=?", (operation_id,)
            ).fetchone()
            if existing is not None:
                if any(existing[name] != value for name, value in identity.items()):
                    raise Conflict("Run receipt is immutable and conflicts with the recorded one.")
                return 200, _with_data_classes(connection, _public(existing))
            now = utc_now()
            row = identity | {
                "operation_id": operation_id,
                "org_id": operation["org_id"],
                "project_id": operation["project_id"],
                "task_id": operation["task_id"],
                "grant_id": operation["grant_id"],
                "lease_id": operation["lease_id"],
                "epoch": operation["epoch"],
                "worker_principal_id": worker["id"],
                "provider_kind": provider_kind,
                "receipt_evidence": RECEIPT_EVIDENCE,
                "adapter_version": operation["adapter_version"],
                "duration_ms": duration_ms,
                "receipt_payload_json": canonical,
                "provider_success": "not_established",
                "scientific_validity": "not_assessed",
                "created_at": now,
            }
            try:
                connection.execute(
                    """INSERT INTO execution_run_receipts
                    (operation_id, org_id, project_id, task_id, grant_id, lease_id, epoch,
                    worker_principal_id, provider_kind, receipt_evidence, adapter_version,
                    provider_id, command_digest, exit_code, started_at, finished_at, duration_ms,
                    output_digest, receipt_payload_json, receipt_digest, provider_success,
                    scientific_validity, created_at)
                    VALUES (:operation_id,:org_id,:project_id,:task_id,:grant_id,:lease_id,:epoch,
                    :worker_principal_id,:provider_kind,:receipt_evidence,:adapter_version,
                    :provider_id,:command_digest,:exit_code,:started_at,:finished_at,:duration_ms,
                    :output_digest,:receipt_payload_json,:receipt_digest,:provider_success,
                    :scientific_validity,:created_at)""",
                    row,
                )
            except sqlite3.IntegrityError as error:
                raise Conflict("Run receipt conflicts with existing evidence.") from error
            # Observed state stays owned by the reconciliation projection over worker,
            # receipt and recovery evidence; a run receipt only binds provider identity.
            connection.execute(
                """UPDATE external_operations SET provider_id=coalesce(provider_id, ?),
                updated_at=? WHERE operation_id=?""",
                (payload["provider_id"], now, operation_id),
            )
            self._event(
                connection,
                worker,
                operation["project_id"],
                operation["task_id"],
                "worker.execution_run_receipt_recorded",
                operation["epoch"],
                {
                    "operation_id": operation_id,
                    "provider_kind": provider_kind,
                    "exit_code": payload["exit_code"],
                    "receipt_digest": payload["receipt_digest"],
                    "receipt_evidence": RECEIPT_EVIDENCE,
                    "provider_success": "not_established",
                    "scientific_validity": "not_assessed",
                    **grant_input_classes(connection, grant["id"]),
                    **_lease_trust_fields(connection, operation["org_id"], operation["lease_id"]),
                    "output_data_class": OUTPUT_DATA_CLASS,
                },
            )
            return 201, _with_data_classes(connection, _public(row))

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")

    def get_run_receipt(self, actor: dict[str, Any], operation_id: str) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            live = self._live_principal(connection, actor)
            row = connection.execute(
                "SELECT * FROM execution_run_receipts WHERE operation_id=? AND org_id=?",
                (operation_id, live["org_id"]),
            ).fetchone()
            if row is None or (
                live["project_id"] is not None and live["project_id"] != row["project_id"]
            ):
                raise NotFound("Run receipt was not found.")
            return _with_data_classes(connection, _public(row))
        finally:
            connection.close()
