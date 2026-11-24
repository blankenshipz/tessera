"""Shared synthetic setup for campaign tests (ADR 0038); experiment-agnostic."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_execution import _setup

from tessera_control.approval_trust import AUTOMATION_KEY
from tessera_control.campaign_delegation import (
    STEP_TABLE,
    delegated_principal,
    intent_key,
    write_intent,
)
from tessera_control.campaign_store import CampaignStore
from tessera_control.store import Store, _connect, _token_hash, canonical_digest


def declare_snapshot_public(state: Path, snapshot_id: str) -> None:
    """Synthetic fixture only: record a snapshot as human-declared public (ADR 0039)."""
    import sqlite3

    from data_class_fixtures import restore_class_guards, rewrite_class_row

    with sqlite3.connect(state / "tessera.sqlite3") as db:
        actor = db.execute(
            "SELECT actor_principal_id FROM source_snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()[0]
        rewrite_class_row(db, snapshot_id)
        db.execute(
            """INSERT INTO snapshot_data_classes (snapshot_id, data_class, origin, license,
            source, actor_principal_id, decision_class, recorded_at)
            VALUES (?, 'public', 'human_declared', 'CC0-1.0',
            'https://example.org/synthetic-fixture', ?, 'owner_automation_labelled',
            '2026-01-01T00:00:00Z')""",
            (snapshot_id, actor),
        )
        restore_class_guards(db)


def record_snapshot_private(state: Path, snapshot_id: str, origin: str = "derived") -> None:
    """Synthetic fixture only: re-record a snapshot as private_derived (ADR 0039)."""
    import sqlite3

    from data_class_fixtures import restore_class_guards, rewrite_class_row

    with sqlite3.connect(state / "tessera.sqlite3") as db:
        rewrite_class_row(db, snapshot_id)
        db.execute(
            """INSERT INTO snapshot_data_classes (snapshot_id, data_class, origin, recorded_at)
            VALUES (?, 'private_derived', ?, '2026-01-01T00:00:00Z')""",
            (snapshot_id, origin),
        )
        restore_class_guards(db)


IMAGE = "python@sha256:" + "a" * 64


class Env:
    """Project with a worker, one adopted+released snapshot, and a campaign store."""

    def __init__(self, tmp_path: Path) -> None:
        self.state, self.execution, self.owner, self.worker, self.project, _task = _setup(tmp_path)
        self.store = CampaignStore(self.state)
        self.plain = Store(self.state)
        with _connect(self.state / "tessera.sqlite3") as connection:
            row = connection.execute("SELECT id, digest FROM source_snapshots").fetchone()
        self.snapshot_id, self.snapshot_digest = row["id"], row["digest"]
        declare_snapshot_public(self.state, self.snapshot_id)
        # The builtin trust setting admits public data only (ADR 0040 section 4); campaign
        # tests with stage-fed (derived) inputs bind the test worker to the local provider.
        self.trust()
        self.release_id = self.query("SELECT id FROM snapshot_releases")[0]["id"]
        self.counter = 0
        self._actors: dict[str, dict[str, Any]] = {}

    def trust(self, classes=None, provider: str = "local-docker") -> None:
        from provider_trust_fixture import DERIVED, install_trust

        install_trust({provider: (DERIVED if classes is None else classes, [self.worker["id"]])})

    def query(self, sql: str, *args: Any) -> list[Any]:
        connection = _connect(self.state / "tessera.sqlite3")
        try:
            return connection.execute(sql, args).fetchall()
        finally:
            connection.close()

    def execute(self, sql: str, *args: Any) -> None:
        connection = _connect(self.state / "tessera.sqlite3")
        try:
            connection.execute(sql, args)
        finally:
            connection.close()

    def human(self, name: str = "human") -> dict[str, Any]:
        """A separately issued human_session credential (no automation label needed)."""
        if name in self._actors:
            return self._actors[name]
        body = {
            "kind": "human_session",
            "audience": "control",
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
        token = f"synthetic-{name}-token-" + "x" * 30
        self.plain.issue_credential(self.owner, f"cred-{name}", canonical_digest(body), body, token)
        self._actors[name] = self.plain.authenticate(token)
        return self._actors[name]

    def agent(self, actions: list[str] | None = None, name: str = "agent") -> dict[str, Any]:
        if name in self._actors:
            return self._actors[name]
        body: dict[str, Any] = {
            "kind": "agent",
            "audience": "control",
            "project_id": self.project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        }
        if actions is not None:
            body["external_agent"] = {"actions": actions, "destination": "planner.local"}
        token = f"synthetic-{name}-token-" + "x" * 30
        self.plain.issue_credential(self.owner, f"cred-{name}", canonical_digest(body), body, token)
        actor = self.plain.authenticate(token)
        if actions is not None:
            actor = {**actor, "agent_destination": "planner.local"}
        self._actors[name] = actor
        return actor

    def _other_org_owner(self) -> dict[str, Any]:
        """The other organization's root owner and its one project (idempotent, unique ids)."""
        org, stamp = "org_other_agents", "2026-01-01T00:00:00Z"
        owner_token = "synthetic-other-org-owner-token-" + "x" * 30
        with _connect(self.state / "tessera.sqlite3") as connection:
            connection.execute(
                "INSERT OR IGNORE INTO organizations (id, name, created_at) VALUES (?, ?, ?)",
                (org, "Other tenant", stamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO projects (id, org_id, name, created_at) VALUES (?, ?, ?, ?)",
                ("project_other_org", org, "other", stamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO principals (id, org_id, role, token_hash, revoked_at, "
                "created_at) VALUES ('principal_other_org_owner', ?, 'human', ?, NULL, ?)",
                (org, _token_hash(owner_token), stamp),
            )
            connection.execute(
                "INSERT OR IGNORE INTO credentials (id, principal_id, kind, audience, project_id, "
                "parent_id, expires_at, revoked_at, created_at, created_by_principal_id) VALUES "
                "('credential_other_org_owner', 'principal_other_org_owner', 'local_owner', "
                "'control', NULL, NULL, NULL, NULL, ?, 'principal_other_org_owner')",
                (stamp,),
            )
        return self.plain.authenticate(owner_token)

    def _issue_other_org(self, name: str, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        owner = self._other_org_owner()
        token = f"synthetic-{name}-token-" + "x" * 30
        self.plain.issue_credential(owner, f"cred-{name}", canonical_digest(body), body, token)
        return self.plain.authenticate(token), token

    def other_org_agent(
        self, actions: list[str] | None = None, name: str = "other-org-agent"
    ) -> dict[str, Any]:
        """An agent of ANOTHER organization, bound to that organization's own project, issued
        through the real ``Store.issue_credential``. The result carries its ``token``."""
        key = ("other-org-agent", name)
        if key not in self._actors:
            body: dict[str, Any] = {
                "kind": "agent",
                "audience": "control",
                "project_id": "project_other_org",
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            }
            if actions is not None:
                body["external_agent"] = {"actions": actions, "destination": "planner.local"}
            actor, token = self._issue_other_org(name, body)
            if actions is not None:
                actor = {**actor, "agent_destination": "planner.local"}
            self._actors[key] = {**actor, "token": token}
        return self._actors[key]

    def other_org_human(self, name: str = "other-org-human") -> dict[str, Any]:
        """A ``human_session`` of ANOTHER organization. Its credential has ``project_id`` NULL
        (the only unbound principal the issuing path allows besides a root owner), so no
        project binding can refuse it first: the org filter in ``Store._project`` alone does."""
        key = ("other-org-human", name)
        if key not in self._actors:
            body = {
                "kind": "human_session",
                "audience": "control",
                "expires_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            }
            actor, token = self._issue_other_org(name, body)
            self._actors[key] = {**actor, "token": token}
        return self._actors[key]

    def spec(self, **overrides: Any) -> dict[str, Any]:
        stage1 = {
            "key": "s1",
            "backend": "docker-local-cpu-v1",
            "image": IMAGE,
            "argv": ["python", "-c", "print(1)"],
            "output_paths": ["data.json"],
            "max_output_bytes": 1024,
            "max_duration_seconds": 60,
            "max_exposure_cents": 6,
            "inputs": [
                {
                    "kind": "snapshot",
                    "name": "code",
                    "snapshot_id": self.snapshot_id,
                    "digest": self.snapshot_digest,
                    "data_class": "public",
                }
            ],
            "metrics": [{"name": "score", "path": "data.json", "json_pointer": "/score"}],
        }
        stage2 = {
            **stage1,
            "key": "s2",
            "inputs": [
                stage1["inputs"][0],
                {"kind": "stage", "name": "up", "stage": "s1", "paths": ["data.json"]},
            ],
        }
        spec = {
            "version": 1,
            "name": "synthetic campaign",
            "budget_cap_cents": 10,
            "deadline_seconds": 3600,
            "worker_principal_id": self.worker["id"],
            "stages": [stage1, stage2],
        }
        spec.update(overrides)
        return spec

    def submit(self, actor: dict[str, Any] | None = None, spec: dict | None = None) -> dict:
        body = {"spec": spec or self.spec()}
        self.counter += 1
        status, result = self.store.submit_campaign(
            actor or self.owner,
            self.project["id"],
            f"submit-{self.counter}",
            canonical_digest(body),
            body,
        )
        assert status == 201
        return result

    def approve(self, campaign: dict[str, Any], actor: dict[str, Any] | None = None) -> dict:
        body = {
            "expected_revision": campaign["revision"],
            "expected_spec_digest": campaign["spec_digest"],
        }
        self.counter += 1
        status, result = self.store.approve_campaign(
            actor or self.human(),
            campaign["id"],
            f"approve-{self.counter}",
            canonical_digest(body),
            body,
        )
        assert status == 200
        return result

    def approved(self, actor: dict[str, Any] | None = None) -> tuple[dict, dict]:
        campaign = self.approve(self.submit(), actor)
        return campaign, campaign["approval"]

    def intent(
        self,
        campaign: dict,
        approval: dict,
        step: str,
        resources: dict[str, str],
        payload: dict[str, Any],
        *,
        stage_key: str | None = "s1",
        attempt: int | None = None,
        ordinal: int = 0,
        path: str | None = None,
        digest: str | None = None,
    ) -> tuple[dict, dict, str, str]:
        """Write an intent the way the runner does; returns (principal, intent, key, digest)."""
        method, template = STEP_TABLE[step]
        path = path or template.format(**resources)
        key = intent_key(campaign["id"], stage_key, attempt, step, ordinal)
        digest = digest or canonical_digest(payload)
        campaign_row = dict(self.query("SELECT * FROM campaigns WHERE id=?", campaign["id"])[0])
        approval_row = dict(
            self.query("SELECT * FROM campaign_approvals WHERE id=?", approval["id"])[0]
        )
        with self.store.runner_connection() as connection:
            intent = write_intent(
                connection,
                campaign=campaign_row,
                approval=approval_row,
                stage_key=stage_key,
                attempt=attempt,
                step=step,
                ordinal=ordinal,
                method=method,
                path=path,
                request_digest=digest,
                idempotency_key=key,
                resources=resources,
            )
        principal = delegated_principal(approval_row, campaign_row, intent["id"])
        return principal, intent, key, digest

    def task_payload(self, title: str = "stage task") -> dict[str, Any]:
        return {
            "title": title,
            "purpose": "Campaign synthetic stage",
            "budget_cents": 12,
            "max_duration_seconds": 60,
        }

    def create_task_step(self, campaign: dict, approval: dict, stage_key: str = "s1") -> dict:
        payload = self.task_payload(f"{campaign['id'][-6:]}/{stage_key}")
        principal, intent, key, digest = self.intent(
            campaign,
            approval,
            "create_task",
            {"project_id": self.project["id"]},
            payload,
            stage_key=stage_key,
        )
        status, task = self.plain.create_task(principal, self.project["id"], key, digest, payload)
        assert status == 201
        self.execute(
            "UPDATE campaign_stages SET task_id=?, prep_phase='task_created' "
            "WHERE campaign_id=? AND stage_key=?",
            task["id"],
            campaign["id"],
            stage_key,
        )
        return task

    def intents(self, campaign_id: str) -> list[Any]:
        return self.query(
            "SELECT * FROM campaign_step_intents WHERE campaign_id=? ORDER BY created_at, id",
            campaign_id,
        )

    def events(self, event_type: str) -> list[dict[str, Any]]:
        return [
            {**dict(row), "payload": json.loads(row["payload_json"])}
            for row in self.query(
                "SELECT * FROM events WHERE event_type=? ORDER BY cursor", event_type
            )
        ]


__all__ = ["AUTOMATION_KEY", "IMAGE", "Env"]
