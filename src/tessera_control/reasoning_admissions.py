"""A person's admission of the reasoning service, as an owner-delegation record (ADR 0163).

The reasoning unit starts only while ``reasoning.admitted`` exists (its ``ConditionPathExists``).
That file used to be an anonymous assertion. This module records the admission behind it: who
admitted the service (a person's ``human_session`` credential, never an agent, worker or the owner
token, labelled or not), for which project, provider and model, under which caps (per call, per
day, tokens per day and optionally a total number of calls) and until when. The marker file then
carries only a non-secret reference to the record (its id and digest), and the first-call
readiness ``admission`` check refuses a recorded marker whose record is revoked, expired, altered
or exceeded by the profile.

Storage reuses the control plane's receipts: the record is the payload of an append-only
``reasoning.admission_recorded`` event and a revocation is a ``reasoning.admission_revoked``
event, each written with its idempotency row and its tenant check in one transaction
(``Store._mutate``). No table and no migration. Like an approval delegation (ADR 0122) the record
is signed once and never edited; the only change is ``active`` to ``revoked``.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import Field, StringConstraints, ValidationError, model_validator

from .approval_trust import decision_provenance, require_decision_actor
from .delegation_terms import MAX_DELEGATION_DAYS, _Strict
from .external_agents import ExternalAgentStore
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    StoreError,
    _connect,
    _parse_utc,
    _utc_string,
    canonical_digest,
    utc_now,
)

RECORDED = "reasoning.admission_recorded"
REVOKED = "reasoning.admission_revoked"
READ_SCOPE = "campaign:read"
ID_PREFIX = "radm_"
RECORD_VERSION = 1
MAX_ADMISSION_DAYS = MAX_DELEGATION_DAYS
MAX_LISTED = 200
# The fields that make up the signed record; the digest covers exactly these.
RECORD_FIELDS = (
    "version",
    "admission_id",
    "org_id",
    "project_id",
    "provider_id",
    "model",
    "caps",
    "expires_at",
    "created_at",
    "admitted_by_principal_id",
    "admitted_credential_id",
    "decision_class",
)

Cents = Annotated[int, Field(strict=True, ge=1, le=10_000_000)]
ProviderId = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,63}$")]
# A bare model id (no provider prefix), the shape a reasoning grant names it by.
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}$")]


class AdmissionCaps(_Strict):
    """The most the admitted service may be configured to spend. The profile's own ``limits``
    must sit at or below these (readiness checks it); the model-spend ledger and the reasoning
    grant still enforce their own caps on every call."""

    max_cost_cents_per_call: Cents
    daily_cost_cap_cents: Cents
    daily_token_cap: Annotated[int, Field(strict=True, ge=1, le=10_000_000_000)]
    # Optional: the most model calls (ledger rows for this project, provider and model, not
    # released) made from the admission's creation on. Absent: no count limit beyond the caps.
    max_calls: Annotated[int, Field(strict=True, ge=1, le=1_000_000)] | None = None

    @model_validator(mode="after")
    def per_call_within_daily(self) -> AdmissionCaps:
        if self.max_cost_cents_per_call > self.daily_cost_cap_cents:
            raise ValueError("max_cost_cents_per_call cannot exceed daily_cost_cap_cents")
        return self


class CreateReasoningAdmission(_Strict):
    provider_id: ProviderId
    model: ModelId
    caps: AdmissionCaps
    expires_at: Annotated[str, Field(min_length=1, max_length=64)]


class RevokeReasoningAdmission(_Strict):
    expected_revision: Annotated[int, Field(strict=True, ge=1)]


def _invalid(error: ValidationError) -> StoreError:
    first = error.errors()[0] if error.errors() else {}
    location = ".".join(str(part) for part in first.get("loc", ()))[:200]
    return StoreError(f"Invalid reasoning admission: {location}: {str(first.get('msg', ''))[:200]}")


def record_digest(record: dict[str, Any]) -> str:
    """The digest of the signed record: exactly ``RECORD_FIELDS``, canonical JSON."""
    return canonical_digest({name: record[name] for name in RECORD_FIELDS})


def admission_state(admission: dict[str, Any], now: datetime) -> str:
    """``revoked`` beats ``expired``; both end the admission (ADR 0122's order)."""
    if admission["status"] == "revoked":
        return "revoked"
    return "expired" if now >= _parse_utc(admission["record"]["expires_at"]) else "active"


def load_admission(
    connection: sqlite3.Connection, project_id: str, admission_id: str, org_id: str | None = None
) -> dict[str, Any] | None:
    """The admission's record and status from its receipts, or None when no record exists.

    ``integrity`` is False when the stored record does not hash to its stored digest, when it is
    recorded twice, or when a revocation does not follow it; a reader must then fail closed.
    Read-only; works on a read-only connection.
    """
    rows = connection.execute(
        """SELECT cursor, org_id, event_type, resource_revision, actor_principal_id, created_at,
        payload_json FROM events
        WHERE project_id=? AND event_type IN (?, ?)
        AND json_extract(payload_json, '$.admission_id')=?
        AND (? IS NULL OR org_id=?) ORDER BY cursor""",
        (project_id, RECORDED, REVOKED, admission_id, org_id, org_id),
    ).fetchall()
    recorded = [row for row in rows if row[2] == RECORDED]
    if not recorded:
        return None
    first = recorded[0]
    payload = json.loads(first[6])
    record = payload.get("record")
    integrity = (
        len(recorded) == 1
        and isinstance(record, dict)
        and set(RECORD_FIELDS) <= set(record)
        and record.get("admission_id") == admission_id
        and record.get("project_id") == project_id
        and record.get("org_id") == first[1]
        and payload.get("record_digest") == record_digest(record)
    )
    revocations = [row for row in rows if row[2] == REVOKED]
    if revocations and (len(revocations) > 1 or revocations[0][0] < first[0]):
        integrity = False
    revoked = revocations[0] if revocations else None
    return {
        "admission_id": admission_id,
        "project_id": project_id,
        "org_id": first[1],
        "record": record if isinstance(record, dict) else {},
        "record_digest": payload.get("record_digest"),
        "integrity": integrity,
        "status": "revoked" if revoked else "active",
        "revision": 2 if revoked else 1,
        "revoked_at": revoked[5] if revoked else None,
        "revoked_by_principal_id": revoked[4] if revoked else None,
        "provenance": {
            key: payload.get(key)
            for key in ("credential_kind", "decision_class", "automation")
            if key in payload
        },
    }


def _summary(admission: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    record = admission["record"]
    return {
        "id": admission["admission_id"],
        "project_id": admission["project_id"],
        "provider_id": record.get("provider_id"),
        "model": record.get("model"),
        "caps": record.get("caps"),
        "expires_at": record.get("expires_at"),
        "created_at": record.get("created_at"),
        "admitted_by_principal_id": record.get("admitted_by_principal_id"),
        "admitted_credential_id": record.get("admitted_credential_id"),
        "decision_class": record.get("decision_class"),
        "record_digest": admission["record_digest"],
        "integrity": admission["integrity"],
        "status": admission["status"],
        "revision": admission["revision"],
        "revoked_at": admission["revoked_at"],
        "revoked_by_principal_id": admission["revoked_by_principal_id"],
        "state": admission_state(admission, now or _parse_utc(utc_now()))
        if admission["integrity"]
        else "invalid",
    }


class ReasoningAdmissionCommands(ExternalAgentStore):
    """Admission records over the control store's transactions."""

    def _require_person(self, connection, actor) -> None:
        """Only a separately issued human session admits: an admission is an increase in what
        may run, so neither an agent, a worker nor the owner token (labelled or not) may make it."""
        if (
            actor.get("kind") != "human_session"
            or actor["role"] != "human"
            or connection.execute(
                "SELECT 1 FROM external_agent_scopes WHERE credential_id=?",
                (actor["credential_id"],),
            ).fetchone()
        ):
            raise Forbidden("A person's human_session credential must admit the reasoning service.")
        require_decision_actor(actor, forbidden=Forbidden)

    @staticmethod
    def _require_human_role(actor) -> None:
        """Revocation only reduces what may run: any human-role credential (a person's session or
        the owner token) may revoke; agents and workers may not."""
        if actor.get("kind") == "worker" or actor["role"] != "human":
            raise Forbidden("Only a person or the owner may revoke a reasoning admission.")

    def _reader(self, connection, principal, project_id) -> dict[str, Any]:
        actor = self._actor(connection, principal)
        if actor.get("kind") == "worker":
            raise Forbidden("Workers cannot read reasoning admissions.")
        if connection.execute(
            "SELECT 1 FROM external_agent_scopes WHERE credential_id=?", (actor["credential_id"],)
        ).fetchone():
            self._scope(connection, actor, READ_SCOPE)
        self._project(connection, actor, project_id)
        return actor

    def create_reasoning_admission(self, principal, project_id, key, digest, payload):
        try:
            body = CreateReasoningAdmission.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_person(connection, live)
            created = _parse_utc(utc_now())
            try:
                expires = _parse_utc(body.expires_at)
            except ValueError:
                raise StoreError("expires_at must be an ISO-8601 time with an offset.") from None
            if expires <= created or expires > created + timedelta(days=MAX_ADMISSION_DAYS):
                raise StoreError(f"expires_at must be within {MAX_ADMISSION_DAYS} days from now.")
            provenance = decision_provenance(live)
            record = {
                "version": RECORD_VERSION,
                "admission_id": ID_PREFIX + uuid.uuid4().hex,
                "org_id": live["org_id"],
                "project_id": project_id,
                "provider_id": body.provider_id,
                "model": body.model,
                "caps": body.caps.model_dump(mode="json"),
                "expires_at": _utc_string(expires),
                "created_at": _utc_string(created),
                "admitted_by_principal_id": live["id"],
                "admitted_credential_id": live["credential_id"],
                "decision_class": provenance["decision_class"],
            }
            digest_value = record_digest(record)
            self._event(
                connection,
                live,
                project_id,
                None,
                RECORDED,
                1,
                {
                    "admission_id": record["admission_id"],
                    "record": record,
                    "record_digest": digest_value,
                    **provenance,
                },
            )
            admission = load_admission(connection, project_id, record["admission_id"])
            assert admission is not None and admission["integrity"]
            return 201, _summary(admission, created)

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/reasoning-admissions", digest, command
        )

    def revoke_reasoning_admission(self, principal, project_id, admission_id, key, digest, payload):
        try:
            body = RevokeReasoningAdmission.model_validate(payload)
        except ValidationError as error:
            raise _invalid(error) from None

        def command(connection, live):
            self._project(connection, live, project_id)
            self._require_human_role(live)
            admission = load_admission(connection, project_id, admission_id, live["org_id"])
            if admission is None:
                raise NotFound("Reasoning admission was not found.")
            if admission["revision"] != body.expected_revision:
                raise Conflict("Reasoning admission revision is no longer current.")
            if admission["status"] != "active":
                raise Conflict("Reasoning admission is already revoked.")
            self._event(
                connection,
                live,
                project_id,
                None,
                REVOKED,
                2,
                {
                    "admission_id": admission_id,
                    "record_digest": admission["record_digest"],
                    **decision_provenance(live),
                },
            )
            updated = load_admission(connection, project_id, admission_id, live["org_id"])
            assert updated is not None
            return 200, _summary(updated)

        return self._mutate(
            principal,
            key,
            "POST",
            f"/projects/{project_id}/reasoning-admissions/{admission_id}/revoke",
            digest,
            command,
        )

    def get_reasoning_admission(self, principal, project_id, admission_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, project_id)
            admission = load_admission(connection, project_id, admission_id, actor["org_id"])
            if admission is None:
                raise NotFound("Reasoning admission was not found.")
            return _summary(admission)
        finally:
            connection.close()

    def list_reasoning_admissions(self, principal, project_id) -> dict[str, Any]:
        connection = _connect(self.database_path)
        try:
            actor = self._reader(connection, principal, project_id)
            ids = [
                row[0]
                for row in connection.execute(
                    """SELECT json_extract(payload_json, '$.admission_id') FROM events
                    WHERE project_id=? AND org_id=? AND event_type=? ORDER BY cursor LIMIT ?""",
                    (project_id, actor["org_id"], RECORDED, MAX_LISTED),
                ).fetchall()
            ]
            now = _parse_utc(utc_now())
            listed = []
            for ident in ids:
                admission = load_admission(connection, project_id, ident, actor["org_id"])
                if admission is not None:
                    listed.append(_summary(admission, now))
            return {"reasoning_admissions": listed}
        finally:
            connection.close()


__all__ = [
    "RECORDED",
    "REVOKED",
    "AdmissionCaps",
    "CreateReasoningAdmission",
    "ReasoningAdmissionCommands",
    "RevokeReasoningAdmission",
    "admission_state",
    "load_admission",
    "record_digest",
]
