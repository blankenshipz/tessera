"""Owner-run smoke credentials: a $0, local-docker-only agent credential and its approver.

A smoke test of steering through the MCP tools needs two credentials, as in production: an
enrolled agent credential (external_agent scope for one destination) and a person's session
credential used only for the approval. This module lets the local owner mint exactly that pair
for a short time, as one auditable grant, with the limits enforced by the control plane itself
(not by the smoke script):

* the agent credential is a project-bound ``agent`` credential whose external scope is exactly
  ``SMOKE_ACTIONS`` for the given destination;
* the approver is a project-bound ``human_session`` credential that may do one thing: approve a
  campaign that the paired agent credential submitted (and read);
* every campaign the agent credential submits or validates must use only the local Docker
  provider, a zero budget cap and zero exposure on every stage and candidate, and the approver
  may only approve a campaign with that same shape; anything else is refused with the closed
  reason ``smoke_grant_local_docker_zero_budget_only``;
* the approval event and the campaign receipt record ``smoke_grant_id``;
* both credentials expire after at most ``MAX_TTL_MINUTES`` and ``revoke`` ends them at once.

Tokens are returned to the caller only; the database holds their hashes like any credential.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .campaign_models import LOCAL_DOCKER_PROVIDER, NormalSpec
from .store import (
    Conflict,
    Forbidden,
    NotFound,
    Store,
    _parse_utc,
    _token_hash,
    _utc_string,
    canonical_digest,
    utc_now,
)

REFUSAL = "smoke_grant_local_docker_zero_budget_only"
ROUTE_REFUSAL = "smoke_grant_approval_only"
SCHEMA = "tessera.smoke-credentials.v1"
SMOKE_ACTIONS = (
    "budget:read",
    "campaign:cancel",
    "campaign:read",
    "campaign:submit",
    "snapshot:read",
)
PROVIDERS = (LOCAL_DOCKER_PROVIDER,)
MIN_TTL_MINUTES, MAX_TTL_MINUTES = 1, 60
OWNER_HEADER_NAME = "owner.header"
_APPROVE_PATH = re.compile(r"^/campaigns/[^/]+/approve$")
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def create_smoke_credential_schema(connection: sqlite3.Connection) -> None:
    """Shared by the migration and fresh databases so both converge. Additive, idempotent."""
    connection.execute(
        """CREATE TABLE IF NOT EXISTS smoke_credential_grants (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        agent_credential_id TEXT NOT NULL UNIQUE REFERENCES credentials(id),
        approver_credential_id TEXT NOT NULL UNIQUE REFERENCES credentials(id),
        destination TEXT NOT NULL CHECK(length(destination) BETWEEN 1 AND 200),
        providers_json TEXT NOT NULL CHECK(providers_json = '["local-docker"]'),
        max_budget_cents INTEGER NOT NULL CHECK(max_budget_cents = 0),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        revoked_at TEXT)"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS smoke_credential_grants_no_delete
        BEFORE DELETE ON smoke_credential_grants
        BEGIN SELECT RAISE(ABORT,'smoke_credential_grants rows are never deleted'); END"""
    )
    connection.execute(
        """CREATE TRIGGER IF NOT EXISTS smoke_credential_grants_revoke_only
        BEFORE UPDATE ON smoke_credential_grants
        WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR NEW.id IS NOT OLD.id
          OR NEW.org_id IS NOT OLD.org_id OR NEW.project_id IS NOT OLD.project_id
          OR NEW.agent_credential_id IS NOT OLD.agent_credential_id
          OR NEW.approver_credential_id IS NOT OLD.approver_credential_id
          OR NEW.destination IS NOT OLD.destination
          OR NEW.providers_json IS NOT OLD.providers_json
          OR NEW.max_budget_cents IS NOT OLD.max_budget_cents
          OR NEW.created_by_principal_id IS NOT OLD.created_by_principal_id
          OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
        BEGIN SELECT RAISE(ABORT,'a smoke credential grant can only be revoked, once'); END"""
    )


# -- server-side enforcement ---------------------------------------------------------------


def _grant(connection: sqlite3.Connection, column: str, credential_id: str | None):
    if credential_id is None:
        return None
    return connection.execute(
        f"SELECT * FROM smoke_credential_grants WHERE {column}=?",  # noqa: S608 - fixed names
        (credential_id,),
    ).fetchone()


def agent_grant(connection: sqlite3.Connection, actor: dict[str, Any]):
    return _grant(connection, "agent_credential_id", actor.get("credential_id"))


def approver_grant(connection: sqlite3.Connection, actor: dict[str, Any]):
    return _grant(connection, "approver_credential_id", actor.get("credential_id"))


def _live(grant: sqlite3.Row) -> bool:
    return grant["revoked_at"] is None and _parse_utc(grant["expires_at"]) > datetime.now(UTC)


def spec_is_smoke_safe(spec: NormalSpec) -> bool:
    """Local Docker only (own provider, alternates and reroutes) and nothing that costs money."""
    if spec.budget_cap_cents != 0:
        return False
    for stage in spec.stages:
        if stage.max_exposure_cents != 0:
            return False
        if any(provider != LOCAL_DOCKER_PROVIDER for provider in stage.candidate_ids):
            return False
        if any(item.max_exposure_cents not in (None, 0) for item in stage.alternates):
            return False
    return True


def check_submission(
    connection: sqlite3.Connection, actor: dict[str, Any], spec: NormalSpec
) -> None:
    """Submit and validate: a smoke grant's agent credential may only send a $0 local spec."""
    grant = agent_grant(connection, actor)
    if grant is not None and not (_live(grant) and spec_is_smoke_safe(spec)):
        raise Forbidden(
            f"{REFUSAL}: a smoke credential may only submit local-docker, $0 campaigns."
        )


def check_approval(
    connection: sqlite3.Connection, actor: dict[str, Any], campaign: sqlite3.Row, spec: NormalSpec
) -> str | None:
    """Approve: returns the grant id when a smoke approver approves, or None for anyone else."""
    grant = approver_grant(connection, actor)
    if grant is None:
        return None
    if not (
        _live(grant)
        and campaign["project_id"] == grant["project_id"]
        and campaign["submitted_credential_id"] == grant["agent_credential_id"]
        and spec_is_smoke_safe(spec)
    ):
        raise Forbidden(
            f"{REFUSAL}: this approver may only approve a local-docker, $0 campaign "
            "submitted by its paired agent credential."
        )
    return grant["id"]


def check_route(
    connection: sqlite3.Connection, actor: dict[str, Any], method: str, path: str
) -> None:
    """A smoke approver is limited to reads and the campaign approve route."""
    if actor.get("kind") != "human_session" or approver_grant(connection, actor) is None:
        return
    if method.upper() in _READ_METHODS or (
        method.upper() == "POST" and _APPROVE_PATH.fullmatch(path)
    ):
        return
    raise Forbidden(f"{ROUTE_REFUSAL}: a smoke approver credential may only approve campaigns.")


def grant_id_for_principal(connection: sqlite3.Connection, principal_id: str) -> str | None:
    """The smoke grant a decision's principal was the approver of (for receipts)."""
    row = connection.execute(
        """SELECT g.id FROM smoke_credential_grants g
        JOIN credentials c ON c.id = g.approver_credential_id WHERE c.principal_id=?""",
        (principal_id,),
    ).fetchone()
    return None if row is None else row["id"]


# -- owner-run commands --------------------------------------------------------------------


def _owner(state_dir: Path) -> tuple[Store, dict[str, Any]]:
    """Authenticate as the state directory's root owner from its owner header (mode 0600)."""
    header = Path(state_dir).expanduser() / OWNER_HEADER_NAME
    try:
        if header.stat().st_mode & 0o077:
            raise Forbidden("The owner header must be owner-only (0600).")
        text = header.read_text(encoding="utf-8").strip()
    except OSError:
        raise Forbidden("No owner credential in this state directory.") from None
    prefix = "Authorization: Bearer "
    if not text.startswith(prefix):
        raise Forbidden("The owner header is malformed.")
    store = Store(Path(state_dir).expanduser())
    actor = store.authenticate(text.removeprefix(prefix))
    if not store._is_root_owner(actor):
        raise Forbidden("Only the local owner can manage smoke credentials.")
    return store, actor


def _insert_credential(
    connection: sqlite3.Connection,
    actor: dict[str, Any],
    *,
    kind: str,
    project_id: str,
    token: str,
    expires_at: str,
    created_at: str,
) -> str:
    principal_id, credential_id = "principal_" + uuid.uuid4().hex, "credential_" + uuid.uuid4().hex
    connection.execute(
        "INSERT INTO principals (id, org_id, role, token_hash, revoked_at, created_at)"
        " VALUES (?, ?, ?, ?, NULL, ?)",
        (
            principal_id,
            actor["org_id"],
            "human" if kind == "human_session" else "agent",
            _token_hash(token),
            created_at,
        ),
    )
    connection.execute(
        """INSERT INTO credentials (id, principal_id, kind, audience, project_id, parent_id,
        expires_at, revoked_at, created_at, created_by_principal_id)
        VALUES (?, ?, ?, 'control', ?, ?, ?, NULL, ?, ?)""",
        (
            credential_id,
            principal_id,
            kind,
            project_id,
            actor["credential_id"],
            expires_at,
            created_at,
            actor["id"],
        ),
    )
    return credential_id


def issue(
    state_dir: Path,
    project_id: str,
    destination: str,
    *,
    ttl_minutes: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mint the agent credential, the approver credential and the grant in one transaction.

    Returns the two tokens (never stored) with the ids and expiry. ``now`` is a test hook.
    """
    from .models import ExternalAgentScope

    if (
        isinstance(ttl_minutes, bool)
        or not isinstance(ttl_minutes, int)
        or not MIN_TTL_MINUTES <= ttl_minutes <= MAX_TTL_MINUTES
    ):
        raise Conflict(f"ttl_minutes must be between {MIN_TTL_MINUTES} and {MAX_TTL_MINUTES}.")
    if not isinstance(destination, str) or not destination.strip():
        raise Conflict("A destination is required.")
    scope = ExternalAgentScope.model_validate(
        {"actions": list(SMOKE_ACTIONS), "destination": destination.strip()}
    )
    store, owner = _owner(state_dir)
    from .smoke_snapshot import ensure_public_snapshot

    # Owner authority, before the grant: a refused adoption leaves no grant and no credentials.
    snapshot = ensure_public_snapshot(state_dir, owner, project_id)
    issued_at = now or datetime.now(UTC)
    expires_at = _utc_string(issued_at + timedelta(minutes=ttl_minutes))
    created_at = _utc_string(issued_at)
    agent_token, approver_token = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    grant_id = "smokegrant_" + uuid.uuid4().hex
    body = {"project_id": project_id, "destination": scope.destination, "grant_id": grant_id}

    def command(connection: sqlite3.Connection, actor: dict[str, Any]) -> tuple[int, dict]:
        if not store._is_root_owner(actor):
            raise Forbidden("Only the local owner can issue smoke credentials.")
        store._project(connection, actor, project_id)
        agent_id = _insert_credential(
            connection,
            actor,
            kind="agent",
            project_id=project_id,
            token=agent_token,
            expires_at=expires_at,
            created_at=created_at,
        )
        connection.execute(
            "INSERT INTO external_agent_scopes VALUES (?, ?, ?)",
            (agent_id, json.dumps(scope.actions), scope.destination),
        )
        approver_id = _insert_credential(
            connection,
            actor,
            kind="human_session",
            project_id=project_id,
            token=approver_token,
            expires_at=expires_at,
            created_at=created_at,
        )
        connection.execute(
            """INSERT INTO smoke_credential_grants (id, org_id, project_id, agent_credential_id,
            approver_credential_id, destination, providers_json, max_budget_cents,
            created_by_principal_id, created_at, expires_at, revoked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)""",
            (
                grant_id,
                actor["org_id"],
                project_id,
                agent_id,
                approver_id,
                scope.destination,
                json.dumps(list(PROVIDERS)),
                actor["id"],
                created_at,
                expires_at,
            ),
        )
        store._audit(
            connection,
            actor,
            "smoke_credentials.issued",
            "allowed",
            {
                "grant_id": grant_id,
                "project_id": project_id,
                "destination": scope.destination,
                "agent_credential_id": agent_id,
                "approver_credential_id": approver_id,
                "actions": list(scope.actions),
                "providers": list(PROVIDERS),
                "max_budget_cents": 0,
                "expires_at": expires_at,
                "snapshot_id": snapshot["snapshot_id"],
            },
            project_id=project_id,
        )
        return 201, {"agent_credential_id": agent_id, "approver_credential_id": approver_id}

    _status, ids = store._mutate(
        owner,
        "smoke-issue-" + uuid.uuid4().hex,
        "POST",
        "/smoke-credentials",
        canonical_digest(body),
        command,
    )
    return {
        "schema": SCHEMA,
        "grant_id": grant_id,
        "project_id": project_id,
        "destination": scope.destination,
        "expires_at": expires_at,
        "providers": list(PROVIDERS),
        "max_budget_cents": 0,
        "actions": list(scope.actions),
        "snapshot_id": snapshot["snapshot_id"],
        "snapshot_digest": snapshot["digest"],
        **ids,
        "agent_token": agent_token,
        "approver_token": approver_token,
    }


def revoke(state_dir: Path, grant_id: str) -> dict[str, Any]:
    """Revoke both credentials and the grant. Idempotent: a repeat changes nothing."""
    store, owner = _owner(state_dir)

    def command(connection: sqlite3.Connection, actor: dict[str, Any]) -> tuple[int, dict]:
        if not store._is_root_owner(actor):
            raise Forbidden("Only the local owner can revoke smoke credentials.")
        grant = connection.execute(
            "SELECT * FROM smoke_credential_grants WHERE id=? AND org_id=?",
            (grant_id, actor["org_id"]),
        ).fetchone()
        if grant is None:
            raise NotFound("Smoke credential grant was not found.")
        revoked_at = grant["revoked_at"]
        if revoked_at is None:
            revoked_at = utc_now()
            for column in ("agent_credential_id", "approver_credential_id"):
                connection.execute(
                    "UPDATE credentials SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                    (revoked_at, grant[column]),
                )
            connection.execute(
                "UPDATE smoke_credential_grants SET revoked_at=? WHERE id=?",
                (revoked_at, grant_id),
            )
            store._audit(
                connection,
                actor,
                "smoke_credentials.revoked",
                "allowed",
                {
                    "grant_id": grant_id,
                    "agent_credential_id": grant["agent_credential_id"],
                    "approver_credential_id": grant["approver_credential_id"],
                },
                project_id=grant["project_id"],
            )
        return 200, {"grant_id": grant_id, "revoked_at": revoked_at}

    _status, result = store._mutate(
        owner,
        "smoke-revoke-" + uuid.uuid4().hex,
        "POST",
        f"/smoke-credentials/{grant_id}/revoke",
        canonical_digest({"grant_id": grant_id}),
        command,
    )
    return result


def list_grants(state_dir: Path) -> list[dict[str, Any]]:
    """Every grant, newest first, with its state; never a token."""
    from .store import _connect

    store, owner = _owner(state_dir)
    connection = _connect(store.database_path)
    try:
        rows = connection.execute(
            "SELECT * FROM smoke_credential_grants WHERE org_id=? ORDER BY created_at DESC, id",
            (owner["org_id"],),
        ).fetchall()
    finally:
        connection.close()
    now = datetime.now(UTC)
    return [
        {
            "grant_id": row["id"],
            "project_id": row["project_id"],
            "destination": row["destination"],
            "agent_credential_id": row["agent_credential_id"],
            "approver_credential_id": row["approver_credential_id"],
            "providers": json.loads(row["providers_json"]),
            "max_budget_cents": row["max_budget_cents"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "revoked_at": row["revoked_at"],
            "state": "revoked"
            if row["revoked_at"]
            else "expired"
            if _parse_utc(row["expires_at"]) <= now
            else "active",
        }
        for row in rows
    ]
