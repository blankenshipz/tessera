"""Record a worker's egress journal summary as one append-only event (ADR 0034 s3).

The journal bytes stay in worker custody. The control plane records only the
digest and a bounded summary, and refuses summaries that contradict the frozen
plan allowlist. No table is added: the record is an event.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from pydantic import ValidationError

from .egress_models import EgressPolicy, ReportEgressJournal
from .execution import ExecutionStore
from .migrations import plan_payload
from .store import Conflict, NotFound, StoreError, canonical_digest

EVENT_TYPE = "execution.egress_journal_recorded"
LOCAL_CPU_ADAPTER = "docker-local-cpu-v1"
SANDBOX_ADAPTER = "agent-sandbox-v1"


def egress_journal_for_operation(
    connection: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    """Return the recorded event payload for an operation, if any."""
    row = connection.execute(
        """SELECT payload_json FROM events WHERE event_type = ?
        AND json_extract(payload_json, '$.operation_id') = ? ORDER BY rowid LIMIT 1""",
        (EVENT_TYPE, operation_id),
    ).fetchone()
    return None if row is None else json.loads(row["payload_json"])


def frozen_egress(
    store: ExecutionStore,
    connection: sqlite3.Connection,
    worker: dict[str, Any],
    operation: sqlite3.Row | dict[str, Any],
) -> tuple[str, dict[str, Any] | None]:
    """Derive the network profile from the digest-bound approved plan.

    Packages are recomputed, not trusted, so the approved plan is the record.
    """
    grant = store._grant(connection, worker, operation["grant_id"])
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
    if spec.get("egress") is None:
        return "none", None
    try:
        return "allowlist-proxy-v1", EgressPolicy.model_validate(spec["egress"]).model_dump(
            mode="json"
        )
    except ValidationError as error:
        raise Conflict("Approved egress policy is invalid.") from error


def check_summary_totals(summary: dict[str, Any]) -> None:
    totals = {
        "connections_allowed": sum(
            d["connections"] for d in summary["destinations"] if d["decision"] == "allow"
        ),
        "connections_denied": sum(
            d["connections"] for d in summary["destinations"] if d["decision"] == "deny"
        ),
        "bytes_up": sum(d["bytes_up"] for d in summary["destinations"]),
        "bytes_down": sum(d["bytes_down"] for d in summary["destinations"]),
        "connections_unclosed": sum(d.get("unclosed", 0) for d in summary["destinations"]),
    }
    if any(summary.get(name, 0) != value for name, value in totals.items()):
        raise StoreError("Egress journal totals must equal the sums over its destinations.")


def record_egress_journal(
    store: ExecutionStore,
    connection: sqlite3.Connection,
    actor: dict[str, Any],
    operation: sqlite3.Row | dict[str, Any],
    journal_sha256: str,
    summary: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Shared recorder: allowlist check, once per operation, cap flag, one event."""
    check_summary_totals(summary)
    if operation["adapter_version"] not in {LOCAL_CPU_ADAPTER, SANDBOX_ADAPTER}:
        raise Conflict("Egress journals are accepted only for local-cpu and Sandbox operations.")
    allowed = {(item["host"], item["port"]) for item in policy["destinations"]}
    for item in summary["destinations"]:
        if item["decision"] == "allow" and (item["host"], item["port"]) not in allowed:
            raise Conflict("Egress journal allowed a destination outside the frozen allowlist.")
    operation_id = operation["operation_id"]
    if egress_journal_for_operation(connection, operation_id) is not None:
        raise Conflict("An egress journal was already recorded for this operation.")
    event = {
        "operation_id": operation_id,
        "network_profile": "allowlist-proxy-v1",
        "journal_sha256": journal_sha256,
        "summary": summary,
        "cap_exceeded": summary["bytes_up"] + summary["bytes_down"] > policy["max_egress_bytes"],
    }
    store._event(
        connection,
        actor,
        operation["project_id"],
        operation["task_id"],
        EVENT_TYPE,
        operation["epoch"],
        event,
    )
    return event


class EgressJournalStore(ExecutionStore):
    def _frozen_egress(
        self, connection: sqlite3.Connection, worker: dict[str, Any], operation: sqlite3.Row
    ) -> tuple[str, dict[str, Any] | None]:
        return frozen_egress(self, connection, worker, operation)

    def report_egress_journal(
        self,
        principal: dict[str, Any],
        operation_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        payload = self._body(ReportEgressJournal, payload)
        summary = payload["summary"]
        check_summary_totals(summary)
        path = f"/worker/operations/{operation_id}/egress-journal"

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
                raise Conflict("Egress journal does not match the external operation.")
            lease = connection.execute(
                "SELECT * FROM execution_leases WHERE id=?", (operation["lease_id"],)
            ).fetchone()
            if (
                lease is None
                or lease["epoch"] != payload["expected_epoch"]
                or operation["epoch"] != lease["epoch"]
            ):
                raise Conflict("Egress journal epoch is no longer current.")
            if operation["adapter_version"] != LOCAL_CPU_ADAPTER:
                # Sandbox journals are read by the control plane itself, never reported.
                raise Conflict("Egress journals are accepted only for local-cpu operations.")
            profile, policy = frozen_egress(self, connection, worker, operation)
            if profile != "allowlist-proxy-v1" or policy is None:
                raise Conflict("Operation package does not use the allowlist egress profile.")
            event = record_egress_journal(
                self, connection, worker, operation, payload["journal_sha256"], summary, policy
            )
            return 201, event | {"recorded": True}

        return self._mutate(principal, key, "POST", path, digest, command, audience="worker")
