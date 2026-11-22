"""Synthetic gate-evidence fixture: no provider, dispatch, credential or real science.

Admitted attempts are inserted directly (foreign keys off for the absent task/grant
parents) because these tests exercise evidence binding, not admission or dispatch;
admission itself is covered by the goal-to-decision suites. Numbers are fixtures.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from test_goal_to_decision import _command, _design

from tessera_control.api import create_app
from tessera_control.experiments import ExperimentStore
from tessera_control.gate_evidence import GateEvidenceStore
from tessera_control.store import Store, canonical_digest, initialize_state, utc_now


class GateWorld:
    def __init__(self, tmp_path: Path) -> None:
        self.state = tmp_path / "state"
        initialize_state(self.state)
        self.token = (self.state / "owner.header").read_text().split("Bearer ", 1)[1].strip()
        self.headers = {"Authorization": f"Bearer {self.token}"}
        self.owner = Store(self.state).authenticate(self.token)
        self.client = TestClient(create_app(self.state))
        self.project = self.post("/projects", {"name": "Synthetic gate-evidence fixture"})
        self.experiments = ExperimentStore(self.state)
        self.gates = GateEvidenceStore(self.state)
        self.experiment = _command(
            self.experiments.create_experiment, self.owner, self.project["id"], payload=_design()
        )
        variants = self.experiment["design"]["variants"]
        self.baseline = next(v for v in variants if v["kind"] == "baseline")
        self.intervention = next(v for v in variants if v["kind"] == "intervention")

    def post(self, path, body, *, status=None, headers=None, key=None):
        response = self.client.post(
            path,
            json=body,
            headers=(headers or self.headers) | {"Idempotency-Key": key or uuid.uuid4().hex},
        )
        if status is None:
            assert response.status_code in (200, 201), response.text
        else:
            assert response.status_code == status, response.text
        return response.json()

    def get(self, path, *, status=200, headers=None):
        response = self.client.get(path, headers=headers or self.headers)
        assert response.status_code == status, response.text
        return response.json()

    def db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.state / "tessera.sqlite3")
        connection.row_factory = sqlite3.Row
        return connection

    def admitted_attempt(self, variant: dict | None = None) -> str:
        """Insert one synthetic admitted attempt in state 'admitted' (pre-dispatch)."""
        variant = variant or self.baseline
        ident = "attempt_" + uuid.uuid4().hex
        now = utc_now()
        with self.db() as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                """INSERT INTO research_experiment_admitted_attempts
                (id,experiment_id,design_revision,variant_id,task_id,plan_digest,grant_id,
                 state,revision,created_at,updated_at,created_by_principal_id)
                VALUES (?,?,?,?,?,?,?,'admitted',1,?,?,?)""",
                (
                    ident,
                    self.experiment["id"],
                    self.experiment["current_design_revision"],
                    variant["id"],
                    "task_synthetic_" + ident,
                    "e" * 64,
                    "grant_synthetic_" + ident,
                    now,
                    now,
                    self.owner["id"],
                ),
            )
        return ident

    def set_outcome(self, attempt_id: str, outcome: str, reason: str | None = None) -> None:
        """Synthetic terminal accounting; real resolution is tested elsewhere."""
        with self.db() as connection:
            connection.execute(
                """UPDATE research_experiment_admitted_attempts
                SET state=?,outcome=?,reason=?,resolved_by_principal_id=? WHERE id=?""",
                (
                    outcome,
                    outcome,
                    reason if outcome != "succeeded" else None,
                    self.owner["id"],
                    attempt_id,
                ),
            )

    def revision(self, attempt_id: str) -> int:
        with self.db() as connection:
            return connection.execute(
                "SELECT revision FROM research_experiment_admitted_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()[0]

    def human_measurement(self, attempt_id: str, number: float, name: str = "rmse") -> dict:
        payload = {
            "expected_revision": self.revision(attempt_id),
            "name": name,
            "value": {"number": number},
        }
        status, row = self.experiments.record_measurement(
            self.owner, attempt_id, uuid.uuid4().hex, canonical_digest(payload), payload
        )
        assert status == 201
        return row

    def mark_derived(self, measurement_id: str) -> None:
        """Synthetic stand-in for the retained-output derivation path (tested elsewhere)."""
        with self.db() as connection:
            connection.execute(
                "UPDATE research_admitted_attempt_measurements SET source='derived' WHERE id=?",
                (measurement_id,),
            )
