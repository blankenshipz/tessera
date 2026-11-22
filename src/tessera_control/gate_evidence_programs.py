"""Research program -> outcome entity with a read-only spend roll-up.

The roll-up only sums Tessera's own budget-ledger records for experiments that the
outcome's study variants name in metadata. It is not provider billing and never
writes to a budget table.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any

from .gate_evidence_models import AttachProgramOutcome, CreateResearchProgram
from .store import Conflict, NotFound, utc_now

SPEND_BASIS = "tessera_budget_ledger_records_not_provider_billing"
_SUM_KEYS = ("recorded_cents", "held_cents", "settled_cents")


def _totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {key: sum(row[key] for row in rows) for key in _SUM_KEYS}
    result["ceiling_cents"] = sum(row["ceiling_cents"] or 0 for row in rows)
    result["experiment_count"] = len(rows)
    result["experiments_without_budget"] = sum(1 for r in rows if r["budget_state"] == "no_budget")
    return result


class ResearchProgramMixin:
    """Tenant-scoped research programs mapping outcomes to studies."""

    @staticmethod
    def _program(
        connection: sqlite3.Connection, actor: dict[str, Any], program_id: str
    ) -> dict[str, Any]:
        row = connection.execute(
            """SELECT g.* FROM gate_research_programs g JOIN projects p ON p.id=g.project_id
            WHERE g.id=? AND p.org_id=? AND (? IS NULL OR g.project_id=?)""",
            (program_id, actor["org_id"], actor["project_id"], actor["project_id"]),
        ).fetchone()
        if row is None:
            raise NotFound("Research program was not found.")
        return dict(row)

    @staticmethod
    def _experiment_spend(connection: sqlite3.Connection, experiment_id: str) -> dict[str, Any]:
        budget = connection.execute(
            "SELECT state, ceiling_cents FROM experiment_budgets WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        recorded = connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) FROM experiment_budget_entries "
            "WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()[0]
        held, settled = connection.execute(
            "SELECT COALESCE(SUM(held_cents),0), COALESCE(SUM(settled_cents),0) "
            "FROM experiment_budget_reservations WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        return {
            "experiment_id": experiment_id,
            "budget_state": budget["state"] if budget else "no_budget",
            "ceiling_cents": budget["ceiling_cents"] if budget else None,
            "recorded_cents": recorded,
            "held_cents": held,
            "settled_cents": settled,
        }

    def _program_detail(
        self, connection: sqlite3.Connection, program: dict[str, Any]
    ) -> dict[str, Any]:
        attached = {
            row["outcome"]: dict(row)
            for row in connection.execute(
                "SELECT outcome,study_id,title,created_at,created_by_principal_id "
                "FROM gate_research_program_outcomes WHERE program_id=?",
                (program["id"],),
            )
        }
        outcomes: dict[str, Any] = {}
        all_experiments: dict[str, dict[str, Any]] = {}
        # Outcome ids are program data: only attached outcomes are listed.
        for outcome in sorted(attached):
            record = attached[outcome]
            experiment_ids: set[str] = set()
            for variant in connection.execute(
                "SELECT metadata_json FROM research_variants WHERE study_id=?",
                (record["study_id"],),
            ):
                value = json.loads(variant["metadata_json"]).get("experiment_id")
                if isinstance(value, str):
                    experiment_ids.add(value)
            rows, unresolved = [], []
            for experiment_id in sorted(experiment_ids):
                owned = connection.execute(
                    "SELECT 1 FROM research_experiments WHERE id=? AND project_id=?",
                    (experiment_id, program["project_id"]),
                ).fetchone()
                if owned is None:
                    # Reported, never silently dropped: its spend is not in the roll-up.
                    unresolved.append(experiment_id)
                    continue
                row = self._experiment_spend(connection, experiment_id)
                rows.append(row)
                all_experiments[experiment_id] = row
            outcomes[outcome] = {
                **record,
                "experiments": rows,
                "totals": _totals(rows),
                "unresolved_experiment_links": unresolved,
            }
        distinct = [all_experiments[key] for key in sorted(all_experiments)]
        return {
            **program,
            "outcomes": outcomes,
            "spend": {
                "spend_basis": SPEND_BASIS,
                # Distinct experiments, so one experiment linked from two outcomes
                # is counted once at program level.
                "totals": _totals(distinct),
                "experiments_without_budget": sum(
                    1 for row in distinct if row["budget_state"] == "no_budget"
                ),
            },
        }

    def create_research_program(
        self,
        principal: dict[str, Any],
        project_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | CreateResearchProgram,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(CreateResearchProgram, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "create a research program")
            self._project(connection, actor, project_id)
            program = {
                "id": "program_" + uuid.uuid4().hex,
                "project_id": project_id,
                "name": body["name"],
                "objective": body["objective"],
                "created_at": utc_now(),
                "created_by_principal_id": actor["id"],
            }
            try:
                connection.execute(
                    """INSERT INTO gate_research_programs
                    (id,project_id,name,objective,created_at,created_by_principal_id)
                    VALUES (:id,:project_id,:name,:objective,:created_at,
                    :created_by_principal_id)""",
                    program,
                )
            except sqlite3.IntegrityError as error:
                raise Conflict("A research program with this name already exists.") from error
            self._event_for(
                connection,
                actor,
                program,
                "gate.research_program_created",
                1,
                {"program_id": program["id"]},
            )
            return 201, self._program_detail(connection, program)

        return self._mutate(
            principal, key, "POST", f"/projects/{project_id}/research-programs", digest, command
        )

    def attach_program_outcome(
        self,
        principal: dict[str, Any],
        program_id: str,
        key: str,
        digest: str,
        payload: dict[str, Any] | AttachProgramOutcome,
    ) -> tuple[int, dict[str, Any]]:
        body = self._payload(AttachProgramOutcome, payload)

        def command(
            connection: sqlite3.Connection, actor: dict[str, Any]
        ) -> tuple[int, dict[str, Any]]:
            self._human(actor, "attach a program outcome")
            program = self._program(connection, actor, program_id)
            study = self._study(connection, actor, body["study_id"])
            if study["project_id"] != program["project_id"]:
                raise NotFound("Study was not found.")
            try:
                connection.execute(
                    """INSERT INTO gate_research_program_outcomes
                    (program_id,outcome,study_id,title,created_at,created_by_principal_id)
                    VALUES (?,?,?,?,?,?)""",
                    (
                        program_id,
                        body["outcome"],
                        body["study_id"],
                        body["title"],
                        utc_now(),
                        actor["id"],
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise Conflict(
                    "Outcome or study is already attached to this research program."
                ) from error
            self._event_for(
                connection,
                actor,
                program,
                "gate.research_program_outcome_attached",
                1,
                {
                    "program_id": program_id,
                    "outcome": body["outcome"],
                    "study_id": body["study_id"],
                },
            )
            return 201, self._program_detail(connection, program)

        return self._mutate(
            principal, key, "POST", f"/research-programs/{program_id}/outcomes", digest, command
        )

    def get_research_program(self, principal: dict[str, Any], program_id: str) -> dict[str, Any]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)
            return self._program_detail(connection, self._program(connection, actor, program_id))
        finally:
            connection.close()

    def list_research_programs(
        self, principal: dict[str, Any], project_id: str, offset: int = 0, limit: int = 100
    ) -> list[dict[str, Any]]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            actor = self._live_principal(connection, principal)
            self._project(connection, actor, project_id)
            programs = connection.execute(
                "SELECT * FROM gate_research_programs WHERE project_id=? "
                "ORDER BY created_at,id LIMIT ? OFFSET ?",
                (project_id, limit, offset),
            ).fetchall()
            return [self._program_detail(connection, dict(row)) for row in programs]
        finally:
            connection.close()
