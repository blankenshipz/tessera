"""A human-issued agent authority grant for one reasoning run, in the real in-process control plane.

Builds on ``test_agent_authority.prepared`` (a project with an approved task, an active execution
grant and an experiment variant, which every agent authority grant must be bound to), then:
enrolls a synthetic agent credential for the run, has the human issue it an agent authority grant
(project, budget, duration, expiry) through ``POST /projects/{id}/agent-authority-grants`` and
returns the exact ids a reasoning profile carries. ``endpoint()`` and ``provenance()`` route the
runtime's own control clients to the same in-process app with the agent's real bearer token, so
every control read (grant, holdout, model spend, provenance) is answered by the real routes.
"""

from __future__ import annotations

import functools
import json
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from test_agent_authority import post

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.provenance_client import ProvenanceClient

DESTINATION = "lab"
# What the reasoning supervisor's control reads need, and nothing more.
AGENT_ACTIONS = ["authority:consume", "campaign:read", "model_spend:record", "provenance:append"]


class ReasoningGrant:
    def __init__(self, prepared, model, *, budget_cents=100, max_duration_seconds=3600):
        self.state, self.client, self.owner, _, self.project, base, _ = prepared
        self.token = "synthetic-reasoning-agent-" + secrets.token_hex(16)
        self.n = 0
        expires = datetime.now(UTC) + timedelta(hours=2)
        credential = post(
            self.client,
            self.owner,
            "/credentials",
            {
                "token": self.token,
                "kind": "agent",
                "audience": "control",
                "project_id": self.project,
                "expires_at": (expires + timedelta(hours=1)).isoformat(),
                "external_agent": {"destination": DESTINATION, "actions": AGENT_ACTIONS},
            },
        )
        self.agent = {"Authorization": "Bearer " + self.token, "X-Agent-Destination": DESTINATION}
        self.bounds = {
            **base,  # the approved experiment binding every grant is pinned to
            "subject_credential_id": credential["credential_id"],
            "destination": DESTINATION,
            "model": model,
            "purpose": "bounded reasoning run",
            "actions": ["attempt:status", "authority:delegate"],
            "budget_cents": budget_cents,
            "max_duration_seconds": max_duration_seconds,
            "expires_at": expires.isoformat(),
            "delegation_depth": 1,
        }
        self.issued = post(
            self.client,
            self.owner,
            f"/projects/{self.project}/agent-authority-grants",
            {"bounds": self.bounds},
        )

    def profile_fields(self) -> dict:
        """Exactly the issued grant's values, as the reasoning profile names them."""
        g = self.issued
        return {
            "grant_id": g["id"],
            "revision": g["revision"],
            "epoch": g["epoch"],
            "subject_credential_id": g["bounds"]["subject_credential_id"],
            "destination": g["bounds"]["destination"],
            "max_duration_seconds": g["bounds"]["max_duration_seconds"],
            "project_id": g["project_id"],
            "model_spend": {"project_id": g["project_id"]},
        }

    def _key(self):
        self.n += 1
        return {"Idempotency-Key": f"reasoning-grant-{self.n}"}

    def read(self) -> dict:
        """The grant as the agent reads it (the same route the supervisor's fence reads)."""
        response = self.client.get(
            f"/agent-authority-grants/{self.issued['id']}", headers=self.agent
        )
        assert response.status_code == 200, response.text
        return response.json()

    def revoke(self) -> dict:
        """The human revokes the grant through the real route."""
        fence = {
            "expected_revision": self.issued["revision"],
            "expected_epoch": self.issued["epoch"],
        }
        return post(
            self.client,
            self.owner,
            f"/agent-authority-grants/{self.issued['id']}/revoke",
            fence,
            expected=200,
        )

    def delegate_remaining(self) -> dict:
        """The agent delegates the grant's whole remaining budget, leaving the grant exhausted."""
        child = {
            **self.bounds,
            "delegation_depth": 0,
            "budget_cents": self.read()["remaining_budget_cents"],
        }
        body = {"expected_revision": 1, "expected_epoch": 1, "bounds": child}
        return post(
            self.client, self.agent, f"/agent-authority-grants/{self.issued['id']}/delegate", body
        )

    def limits(self, per_call=5, daily=20):
        body = {
            "project_id": self.project,
            "provider_id": "*",
            "per_call_cents": per_call,
            "daily_cents": daily,
        }
        response = self.client.put(
            "/model-spend/limits", json=body, headers={**self.owner, **self._key()}
        )
        assert response.status_code == 200, response.text

    def endpoint(self, seen: list):
        """A ``JsonEndpoint`` (real constructor checks and headers) answered by the in-process
        app; each call is recorded as (method, path without query, status)."""
        client = self.client

        class InProcess(rr.JsonEndpoint):
            def call(self, method, path, body=None, key=None):
                headers = {**self.headers, **({"Idempotency-Key": key} if key else {})}
                content = None if body is None else json.dumps(body, allow_nan=False)
                response = client.request(method, path, content=content, headers=headers)
                seen.append((method, path.split("?")[0], response.status_code))
                if response.status_code >= 400:
                    raise rr.RuntimeBlocked("endpoint_unavailable_or_invalid")
                return response.json()

        return InProcess

    def provenance(self):
        return functools.partial(ProvenanceClient, transport=self.client._transport)

    def rows(self, sql, *args):
        with sqlite3.connect(self.state / "tessera.sqlite3") as db:
            db.row_factory = sqlite3.Row
            return db.execute(sql, args).fetchall()
