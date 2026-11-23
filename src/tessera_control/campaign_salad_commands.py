"""Salad worker-audience commands of the campaign store (ADR 0038 amendment).

These commands are the executor's only write path for a Salad attempt. They are never
exposed over HTTP and are mixed into ``CampaignStore`` so the core store module carries no
vendor-specific code.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from .store import Conflict, NotFound, StoreError, _connect, canonical_digest, utc_now


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid campaign request: {location}: {str(first.get('msg', ''))[:200]}")


# -- Salad worker-audience commands (ADR 0038 amendment; never exposed over HTTP) ---------

SALAD_ACTIVE_PHASES = (
    "leased",
    "awaiting_review",
    "reviewed",
    "capability_ready",
    "route_proposed",
    "route_started",
    "executing",
    "outcome_recorded",
    "awaiting_charge",
    "unknown",
)
_WORKER_CAMPAIGN_STATUSES = ("running", "stopping", "cancel_requested")
MAX_LIVE_RECEIPT_BYTES = 65536
_Hex32 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
_Hex64 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
_Ident = Annotated[str, StringConstraints(min_length=1, max_length=200)]


class _SaladPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    operation_id: _Hex32
    lease_id: _Ident
    epoch: Annotated[int, Field(ge=1, le=2**31 - 1)]
    package_digest: _Hex64


_CentsText = Annotated[str, StringConstraints(pattern=r"^[0-9]{1,12}(\.[0-9]{1,12})?$")]


class _PrestartRebind(BaseModel):
    """One re-bind of the guardian to a new allocation before anything ran (bounded text)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    at: Annotated[str, StringConstraints(max_length=40)]
    from_allocation: Annotated[str, StringConstraints(max_length=96)]
    to_allocation: Annotated[str, StringConstraints(max_length=96)]
    reason: Literal["prestart_reallocation"]


class _PrestartRebinds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    count: Annotated[int, Field(ge=1, le=10)]
    events: Annotated[list[_PrestartRebind], Field(max_length=3)]


class _GuardianFinal(BaseModel):
    """The final guardian status projection the executor records (nothing else)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    status: Literal["reserved", "armed", "stopped", "reconciled", "unknown"]
    reason: Annotated[str, StringConstraints(max_length=80)]
    orphan: bool
    allocation_uncertain: bool
    escalation_pending: bool
    budget_fenced: bool
    rate_unknown: bool
    published_hourly_rate_usd: _CentsText | None = None
    estimated_exposure_cents: _CentsText | None = None
    actual_billed_cost_cents: Annotated[int, Field(ge=0)] | None = None
    observed_at: Annotated[str, StringConstraints(max_length=40)]
    # Present only when the guardian re-bound its allocation before anything ran.
    prestart_rebinds: _PrestartRebinds | None = None


class _SaladOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    route_id: _Ident
    live_receipt: dict[str, Any]
    manifest_id: _Ident | None = None
    provider_id: _Ident | None = None
    estimated_exposure: dict[str, Any] | None = None
    guardian_final: _GuardianFinal | None = None


def _salad_attempt(
    connection: sqlite3.Connection, worker: Mapping[str, Any], attempt_id: str
) -> sqlite3.Row:
    """The Salad attempt row for this worker, tenant-checked; refuses a stopped campaign."""
    row = connection.execute(
        """SELECT sa.*, a.campaign_id, a.stage_key, a.grant_id, c.status AS campaign_status,
        c.project_id AS campaign_project_id, g.worker_principal_id AS grant_worker
        FROM campaign_salad_attempts sa
        JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
        JOIN campaigns c ON c.id = a.campaign_id
        LEFT JOIN execution_grants g ON g.id = a.grant_id
        WHERE sa.attempt_id = ? AND c.org_id = ?""",
        (attempt_id, worker["org_id"]),
    ).fetchone()
    if row is None or row["grant_worker"] != worker["id"]:
        raise NotFound("Salad campaign attempt was not found.")
    if row["campaign_status"] not in _WORKER_CAMPAIGN_STATUSES:
        raise Conflict("The campaign is not running; no Salad progress is recorded.")
    return row


def _salad_move(
    store: Any,
    connection: sqlite3.Connection,
    worker: Mapping[str, Any],
    row: sqlite3.Row,
    fields: Mapping[str, Any],
    event: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    now = utc_now()
    names = list(fields)
    assignments = ", ".join(f"{name}=?" for name in names)
    updated = connection.execute(
        f"UPDATE campaign_salad_attempts SET {assignments}, revision=revision+1, updated_at=? "
        "WHERE attempt_id=? AND revision=?",
        (*[fields[name] for name in names], now, row["attempt_id"], row["revision"]),
    ).rowcount
    if updated != 1:
        raise Conflict("Salad attempt changed concurrently.")
    store._event(
        connection,
        worker,
        row["campaign_project_id"],
        None,
        event,
        row["revision"] + 1,
        {"campaign_id": row["campaign_id"], "attempt_id": row["attempt_id"], **payload},
    )
    return {"attempt_id": row["attempt_id"], **fields, "revision": row["revision"] + 1}


class SaladWorkerCommands:
    """Mixin: the Salad executor's package, executing and outcome commands."""

    def record_salad_package(self, worker, attempt_id, key, payload):
        try:
            body = _SaladPackage.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        digest = canonical_digest(dict(payload))

        def command(connection, live):
            row = _salad_attempt(connection, live, attempt_id)
            if row["phase"] != "awaiting_lease":
                raise Conflict("Salad attempt is not awaiting a lease.")
            operation = connection.execute(
                "SELECT * FROM external_operations WHERE operation_id=? AND org_id=?",
                (body.operation_id, live["org_id"]),
            ).fetchone()
            if (
                operation is None
                or operation["grant_id"] != row["grant_id"]
                or operation["worker_principal_id"] != live["id"]
                or operation["lease_id"] != body.lease_id
                or operation["epoch"] != body.epoch
                or operation["package_digest"] != body.package_digest
                or operation["adapter_version"] != "kubernetes-job-v1"
            ):
                raise Conflict("Operation does not match the attempt's grant, lease and package.")
            moved = _salad_move(
                self,
                connection,
                live,
                row,
                {
                    "phase": "leased",
                    "operation_id": body.operation_id,
                    "lease_id": body.lease_id,
                    "epoch": body.epoch,
                    "package_digest": body.package_digest,
                },
                "campaign.salad_package_recorded",
                {"operation_id": body.operation_id, "package_digest": body.package_digest},
            )
            return 200, moved

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/campaign-salad/{attempt_id}/package",
            digest,
            command,
            audience="worker",
        )

    def mark_salad_executing(self, worker, attempt_id, key):
        def command(connection, live):
            row = _salad_attempt(connection, live, attempt_id)
            if row["phase"] != "route_started":
                raise Conflict("Salad attempt has no started route to execute.")
            return 200, _salad_move(
                self,
                connection,
                live,
                row,
                {"phase": "executing"},
                "campaign.salad_executing",
                {"route_id": row["route_id"]},
            )

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/campaign-salad/{attempt_id}/executing",
            canonical_digest({"attempt_id": attempt_id}),
            command,
            audience="worker",
        )

    def record_salad_outcome(self, worker, attempt_id, key, payload):
        try:
            body = _SaladOutcome.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None
        encoded = json.dumps(body.live_receipt, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > MAX_LIVE_RECEIPT_BYTES:
            raise StoreError("Live receipt is too large.")
        digest = canonical_digest(dict(payload))

        def command(connection, live):
            row = _salad_attempt(connection, live, attempt_id)
            if row["phase"] not in ("executing", "route_started"):
                raise Conflict("Salad attempt has no running route to record.")
            if row["route_id"] != body.route_id:
                raise Conflict("Outcome names a route this attempt did not start.")
            exposure = (
                None
                if body.estimated_exposure is None
                else json.dumps(body.estimated_exposure, sort_keys=True)[:2000]
            )
            guardian_final = (
                None
                if body.guardian_final is None
                else json.dumps(
                    {
                        key: value
                        for key, value in body.guardian_final.model_dump().items()
                        # Absent unless used, so the stored shape (and its digest) of every
                        # run without a pre-start re-bind is unchanged.
                        if key != "prestart_rebinds" or value is not None
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            # The control plane computes the digest and infers no status from the receipt.
            return 200, _salad_move(
                self,
                connection,
                live,
                row,
                {
                    "phase": "outcome_recorded",
                    "live_receipt_json": encoded,
                    "live_receipt_digest": canonical_digest(body.live_receipt),
                    "manifest_id": body.manifest_id,
                    "provider_id": body.provider_id,
                    "estimated_exposure": exposure,
                    "guardian_final_json": guardian_final,
                },
                "campaign.salad_outcome_recorded",
                {
                    "route_id": body.route_id,
                    "manifest_id": body.manifest_id,
                    "guardian_final_present": body.guardian_final is not None,
                },
            )

        return self._mutate(
            worker,
            key,
            "POST",
            f"/worker/campaign-salad/{attempt_id}/outcome",
            digest,
            command,
            audience="worker",
        )

    def list_salad_work(self, worker) -> list[dict[str, Any]]:
        """Read-only: this worker's attempts awaiting a lease, holding one, or executing.

        Every phase in which the executor holds the lease is listed (review and route approval
        included) so it can keep the lease renewed until the route has run.
        """
        connection = _connect(self.database_path)
        try:
            rows = connection.execute(
                """SELECT sa.attempt_id, sa.phase, sa.route_id, sa.operation_id, sa.updated_at,
                a.campaign_id, a.stage_key, a.grant_id, a.admitted_attempt_id,
                ss.profile
                FROM campaign_salad_attempts sa
                JOIN campaign_stage_attempts a ON a.id = sa.attempt_id
                JOIN campaigns c ON c.id = a.campaign_id
                JOIN campaign_salad_stages ss
                  ON ss.campaign_id = a.campaign_id AND ss.stage_key = a.stage_key
                JOIN execution_grants g ON g.id = a.grant_id
                WHERE c.org_id = ? AND g.worker_principal_id = ?
                AND sa.phase IN ('awaiting_lease','leased','awaiting_review','reviewed',
                    'capability_ready','route_proposed','route_started','executing')
                AND c.status IN ('running','stopping','cancel_requested')
                ORDER BY sa.updated_at, sa.attempt_id""",
                (worker["org_id"], worker["id"]),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()
