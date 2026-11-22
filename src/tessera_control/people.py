"""Owner-enrolled people: a second person with their own enrolment root.

Only the root owner can enrol a person. The person gets a ``human_session`` credential with no
parent and no creating principal, so review-job separation of duties (``review_jobs``) treats it
as a different person from the owner and from everything the owner issued. The credential cannot
issue credentials, enrol people or act as root owner. The client supplies the token; only its
hash is stored, and neither appears in any audit record or response.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr, field_validator

from .models import URLSAFE_TOKEN_ALPHABET, NonBlank200, StrictRequest
from .request_limits import MAX_TOKEN_CHARS
from .store import (
    MAX_PERSON_CREDENTIAL_LIFETIME,
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _connect,
    _parse_utc,
    _token_hash,
    _utc_string,
    utc_now,
)
from .token_purpose import provider_key_shaped


def create_people_schema(connection: sqlite3.Connection) -> None:
    connection.execute("""CREATE TABLE IF NOT EXISTS enrolled_people (
        principal_id TEXT PRIMARY KEY REFERENCES principals(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        label TEXT NOT NULL,
        enrolled_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        enrolled_at TEXT NOT NULL,
        renewed_at TEXT
    )""")


def _offset_required(value):
    if value.tzinfo is None:
        raise ValueError("expires_at requires an offset")
    return value


class EnrolPerson(StrictRequest):
    token: SecretStr
    label: NonBlank200
    expires_at: datetime

    @field_validator("token")
    @classmethod
    def token_is_urlsafe_and_long(cls, token: SecretStr) -> SecretStr:
        value = token.get_secret_value()
        if provider_key_shaped(value):
            raise ValueError("Provider keys are not Tessera authentication credentials")
        if not 43 <= len(value) <= MAX_TOKEN_CHARS or any(
            character not in URLSAFE_TOKEN_ALPHABET for character in value
        ):
            raise ValueError(f"token must be 43-{MAX_TOKEN_CHARS} URL-safe characters")
        return token

    _expiry_offset = field_validator("expires_at")(lambda cls, v: _offset_required(v))


class RenewPerson(StrictRequest):
    expires_at: datetime

    _expiry_offset = field_validator("expires_at")(lambda cls, v: _offset_required(v))


def _check_expiry(value: str) -> datetime:
    expires_at, now = _parse_utc(value), datetime.now(UTC)
    if expires_at <= now or expires_at > now + MAX_PERSON_CREDENTIAL_LIFETIME:
        raise Conflict("A person's credential expiry must be within the next 7 days.")
    return expires_at


def _response(row: Any) -> dict[str, Any]:
    return {
        "principal_id": row["principal_id"],
        "credential_id": row["credential_id"],
        "label": row["label"],
        "role": "human",
        "kind": "human_session",
        "expires_at": row["expires_at"],
        "enrolled_by_principal_id": row["enrolled_by_principal_id"],
    }


class PeopleStore(Store):
    def _require_owner(self, actor: dict[str, Any], what: str) -> None:
        if not self._is_root_owner(actor):
            raise Forbidden(f"Only a local owner can {what}.")

    def enrol(self, principal, key, digest, payload, token):
        def command(connection, actor):
            self._require_owner(actor, "enrol people")
            expires_at = _check_expiry(payload["expires_at"])
            principal_id = "principal_" + uuid.uuid4().hex
            credential_id = "credential_" + uuid.uuid4().hex
            now = utc_now()
            connection.execute(
                """INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)
                VALUES (?, ?, 'human', ?, NULL, ?)""",
                (principal_id, actor["org_id"], _token_hash(token), now),
            )
            # No parent credential and no creating principal: its own enrolment root.
            connection.execute(
                """INSERT INTO credentials
                (id, principal_id, kind, audience, project_id, parent_id, expires_at,
                 revoked_at, created_at, created_by_principal_id)
                VALUES (?, ?, 'human_session', 'control', NULL, NULL, ?, NULL, ?, NULL)""",
                (credential_id, principal_id, _utc_string(expires_at), now),
            )
            connection.execute(
                """INSERT INTO enrolled_people
                (principal_id, org_id, label, enrolled_by_principal_id, enrolled_at, renewed_at)
                VALUES (?, ?, ?, ?, ?, NULL)""",
                (principal_id, actor["org_id"], payload["label"], actor["id"], now),
            )
            response = _response(
                {
                    "principal_id": principal_id,
                    "credential_id": credential_id,
                    "label": payload["label"],
                    "expires_at": _utc_string(expires_at),
                    "enrolled_by_principal_id": actor["id"],
                }
            )
            self._audit(
                connection,
                actor,
                "person.enrolled",
                "allowed",
                {
                    "principal_id": principal_id,
                    "credential_id": credential_id,
                    "label": payload["label"],
                    "enrolled_by_principal_id": actor["id"],
                    "expires_at": response["expires_at"],
                },
            )
            return 201, response

        return self._mutate(principal, key, "POST", "/people", digest, command)

    def renew(self, principal, person_id, key, digest, payload):
        def command(connection, actor):
            self._require_owner(actor, "renew people")
            row = connection.execute(
                """SELECT e.principal_id, e.label, e.enrolled_by_principal_id, e.org_id,
                c.id AS credential_id FROM enrolled_people e
                JOIN credentials c ON c.principal_id = e.principal_id
                WHERE e.principal_id = ? AND e.org_id = ?""",
                (person_id, actor["org_id"]),
            ).fetchone()
            if row is None:
                raise NotFound("Person was not found.")
            # A revoked or expired credential is never revived by renewal (403).
            person = self._live_principal(
                connection, {"id": row["principal_id"], "org_id": row["org_id"]}
            )
            expires_at = _check_expiry(payload["expires_at"])
            if expires_at <= _parse_utc(person["expires_at"]):
                raise Conflict("Renewal must extend current expiry.")
            now = utc_now()
            connection.execute(
                "UPDATE credentials SET expires_at=? WHERE id=?",
                (_utc_string(expires_at), row["credential_id"]),
            )
            connection.execute(
                "UPDATE enrolled_people SET renewed_at=? WHERE principal_id=?",
                (now, person_id),
            )
            response = _response({**dict(row), "expires_at": _utc_string(expires_at)})
            self._audit(
                connection,
                actor,
                "person.renewed",
                "allowed",
                {
                    "principal_id": person_id,
                    "credential_id": row["credential_id"],
                    "label": row["label"],
                    "expires_at": response["expires_at"],
                },
            )
            return 200, response

        return self._mutate(principal, key, "POST", f"/people/{person_id}/renew", digest, command)

    def list_people(self, principal, offset, limit):
        connection = _connect(self.database_path)
        try:
            actor = self._live_principal(connection, principal)
            self._require_owner(actor, "list people")
            rows = connection.execute(
                """SELECT e.principal_id, c.id AS credential_id, e.label,
                e.enrolled_by_principal_id, e.enrolled_at, c.expires_at, c.revoked_at
                FROM enrolled_people e JOIN credentials c ON c.principal_id = e.principal_id
                WHERE e.org_id = ? ORDER BY e.enrolled_at, e.principal_id LIMIT ? OFFSET ?""",
                (actor["org_id"], limit, offset),
            ).fetchall()
            return [
                {
                    **_response(row),
                    "revoked_at": row["revoked_at"],
                    "enrolled_at": row["enrolled_at"],
                }
                for row in rows
            ]
        finally:
            connection.close()
