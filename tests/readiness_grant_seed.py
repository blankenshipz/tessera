"""A control state directory for the first-call readiness checks: the real control schema, a
model-spend limit and the agent authority grant a reasoning profile names.

``initialize_state`` builds the real schema and the local owner. The agent credential and the
grant are written as rows (the readiness check only reads them), shaped exactly as issuance
writes them: an agent credential enrolled under the owner, and a root reasoning grant
(ADR 0155) issued by the owner. Tests that exercise issuance itself use the real routes
(tests/test_first_call_readiness_grant.py).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tessera_control.store import LOCAL_ORG_ID, initialize_state

GRANT_ID = "grant1"
SUBJECT = "agent1"


def reasoning_bounds(profile: dict, **override) -> dict:
    return {
        "kind": "reasoning",
        "subject_credential_id": profile["subject_credential_id"],
        "destination": profile["destination"],
        "provider_id": profile["model"]["providerID"],
        "model": profile["model"]["id"],
        "purpose": "bounded reasoning run",
        "actions": ["model:call"],
        "bindings": [],
        "budget_cents": 100,
        "max_duration_seconds": 3600,
        "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        "delegation_depth": 0,
        **override,
    }


def seed_control_state(
    control: Path,
    profile: dict,
    *,
    project: str = "proj1",
    limits: tuple[int, int] | None = (20_000_000, 90_000_000),
    bounds: dict | None = None,
) -> Path:
    """Create ``control`` (it must not exist) with a limit row for ``project`` and the grant the
    profile's grant_id/revision/epoch name."""
    initialize_state(control)
    database = sqlite3.connect(control / "tessera.sqlite3")
    try:
        owner_principal, owner_credential = database.execute(
            "SELECT principal_id, id FROM credentials WHERE kind='local_owner'"
        ).fetchone()
        now = datetime.now(UTC)
        if limits is not None:
            database.execute(
                "INSERT INTO model_spend_limits VALUES ('org1', ?, 'anthropic', ?, ?, 1, 'p', "
                "'now')",
                (project, *limits),
            )
        database.execute(
            "INSERT INTO projects VALUES (?, ?, 'readiness', ?)",
            (project, LOCAL_ORG_ID, now.isoformat()),
        )
        database.execute(
            "INSERT INTO principals VALUES ('agent-principal', ?, 'agent', 'synthetic-hash', "
            "NULL, ?)",
            (LOCAL_ORG_ID, now.isoformat()),
        )
        database.execute(
            "INSERT INTO credentials VALUES (?, 'agent-principal', 'agent', 'control', ?, ?, ?, "
            "NULL, ?, ?)",
            (
                profile["subject_credential_id"],
                project,
                owner_credential,
                (now + timedelta(hours=3)).isoformat(),
                now.isoformat(),
                owner_principal,
            ),
        )
        database.execute(
            "INSERT INTO agent_authority_grants VALUES (?, ?, ?, ?, ?, NULL, NULL, ?, 'active', "
            "?, ?, 0, ?, ?)",
            (
                profile["grant_id"],
                LOCAL_ORG_ID,
                project,
                profile["subject_credential_id"],
                owner_principal,
                json.dumps(bounds or reasoning_bounds(profile)),
                profile.get("revision", 1),
                profile.get("epoch", 1),
                now.isoformat(),
                now.isoformat(),
            ),
        )
        database.commit()
    finally:
        database.close()
    return control
