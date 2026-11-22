"""Research program -> outcome entity and the read-only spend roll-up (synthetic)."""

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from gate_evidence_fixture import GateWorld
from pydantic import ValidationError
from test_program_interactions import ProgramJourney

from tessera_control.experiment_budgets import reserve_attempt
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.program_models import ProgramReview
from tessera_control.store import migrate_state

BUDGET_TABLES = (
    "experiment_budgets",
    "experiment_budget_reservations",
    "experiment_budget_entries",
)


def test_platform_has_no_builtin_outcome_catalogs():
    import tessera_control.program_models as models

    assert not hasattr(models, "OUTCOME_CONDITIONS")


def declared(names, selected=1):
    return {
        name: {
            "disposition": "selected" if i < selected else "deferred",
            "rationale": "Plan-declared accounting",
            "variant_ids": ["v1"] if i < selected else [],
        }
        for i, name in enumerate(names)
    }


@pytest.mark.parametrize(
    "outcome",
    ["primary_outcome", "secondary_outcome", "other_outcome", "mixture_transfer", "a", "x" * 64],
)
def test_plan_declares_its_own_conditions_for_any_outcome(tmp_path, outcome):
    scope = dict(ProgramJourney(tmp_path).scope(outcome="primary_outcome"), outcome=outcome)
    ok = dict(scope, conditions=declared(["any_condition", "second_one2"]))
    assert ProgramReview.model_validate(ok).outcome == outcome
    # No catalog: a stored legacy-shaped scope with a subset of conditions is also fine.
    assert ProgramReview.model_validate(dict(scope, conditions=declared(["only_one"])))
    for bad in (
        {},
        declared(["a", "b"], selected=0),
        declared(["Not-Snake"]),
        declared(["has space"]),
        declared(["1leading"]),
        declared(["x" * 65]),
    ):
        with pytest.raises(ValidationError):
            ProgramReview.model_validate(dict(scope, conditions=bad))


@pytest.mark.parametrize("bad", ["Outcome_1", "9outcome", "has space", "x" * 65, "", "a-b"])
def test_bad_outcome_ids_are_rejected(tmp_path, bad):
    scope = ProgramJourney(tmp_path).scope(outcome="primary_outcome")
    with pytest.raises(ValidationError):
        ProgramReview.model_validate(dict(scope, outcome=bad))


def test_program_context_has_no_condition_catalog(tmp_path):
    journey = ProgramJourney(tmp_path)
    assert journey.context(journey.agent)["condition_catalog"] == {}


OLD_OUTCOMES_TABLE = """CREATE TABLE gate_research_program_outcomes (
        program_id TEXT NOT NULL REFERENCES gate_research_programs(id),
        outcome TEXT NOT NULL CHECK (outcome IN ({})),
        study_id TEXT NOT NULL REFERENCES research_studies(id),
        title TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(program_id, outcome), UNIQUE(program_id, study_id))""".format(
    ",".join(f"'outcome_{n}'" for n in range(8))
)


def test_v60_to_v61_drops_outcome_check_and_preserves_rows(tmp_path):
    world = GateWorld(tmp_path)
    made = program(world)
    made_study = study(world)
    path = f"/research-programs/{made['id']}/outcomes"
    # Legacy v60 id: the recreated old CHECK only admits outcome_0..7.
    world.post(path, {"outcome": "outcome_2", "study_id": made_study["id"], "title": "Kept"})
    with world.db() as db:
        fresh_sql = db.execute(
            "SELECT sql FROM sqlite_schema WHERE name='gate_research_program_outcomes'"
        ).fetchone()[0]
        db.execute("PRAGMA foreign_keys=OFF")
        rows = [tuple(r) for r in db.execute("SELECT * FROM gate_research_program_outcomes")]
        db.execute("DROP TABLE gate_research_program_outcomes")
        db.execute(OLD_OUTCOMES_TABLE)
        db.executemany("INSERT INTO gate_research_program_outcomes VALUES (?,?,?,?,?,?)", rows)
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO gate_research_program_outcomes VALUES (?,?,?,?,?,?)",
                (made["id"], "outcome_9", made_study["id"], "t", "now", rows[0][5]),
            )
        db.execute("UPDATE schema_meta SET schema_version=60")
    assert migrate_state(world.state) == LATEST_SCHEMA_VERSION
    with world.db() as db:
        assert (
            db.execute(
                "SELECT sql FROM sqlite_schema WHERE name='gate_research_program_outcomes'"
            ).fetchone()[0]
            == fresh_sql
        )
        assert [
            tuple(r) for r in db.execute("SELECT * FROM gate_research_program_outcomes")
        ] == rows
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute(
            "INSERT INTO gate_research_program_outcomes VALUES (?,?,?,?,?,?)",
            (made["id"], "outcome_9", "other_study", "t", "now", rows[0][5]),
        )


def agent_headers(world: GateWorld, project_id: str | None = None) -> dict:
    token = "synthetic_agent_" + uuid.uuid4().hex + "y" * 16
    world.post(
        "/credentials",
        {
            "token": token,
            "kind": "agent",
            "audience": "control",
            "project_id": project_id or world.project["id"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    )
    return {"Authorization": "Bearer " + token}


def study(world: GateWorld, project_id: str | None = None, experiment_id: str | None = None):
    project_id = project_id or world.project["id"]
    made = world.post(
        f"/projects/{project_id}/studies", {"purpose": "Outcome study", "protocol": "Synthetic"}
    )
    if experiment_id is not None:
        world.post(
            f"/studies/{made['id']}/variants",
            {
                "name": "Linked condition",
                "method_digest": "b" * 64,
                "input_digest": "c" * 64,
                "metadata": {"experiment_id": experiment_id},
            },
        )
    return made


def program(world: GateWorld, name="Program A") -> dict:
    return world.post(
        f"/projects/{world.project['id']}/research-programs",
        {"name": name, "objective": "Synthetic objective"},
    )


def test_create_program_is_idempotent_unique_and_human_only(tmp_path):
    world = GateWorld(tmp_path)
    path = f"/projects/{world.project['id']}/research-programs"
    body = {"name": "Program A", "objective": "Synthetic objective"}
    created = world.post(path, body, key="k1")
    assert world.post(path, body, key="k1") == created
    assert created["outcomes"] == {}
    world.post(path, dict(body, objective="Other"), key="k1", status=409)
    world.post(path, dict(body, objective="Other"), status=409)
    world.post(path, body | {"extra": 1}, status=422)
    world.post(path, body | {"name": "Program B"}, headers=agent_headers(world), status=403)
    with world.db() as db:
        events = db.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='gate.research_program_created'"
        ).fetchone()[0]
    assert events == 1
    assert [p["id"] for p in world.get(path)["research_programs"]] == [created["id"]]


def test_attach_outcome_rules(tmp_path):
    world = GateWorld(tmp_path)
    made = program(world)
    one, two = study(world), study(world)
    path = f"/research-programs/{made['id']}/outcomes"
    attached = world.post(
        path, {"outcome": "tertiary_outcome", "study_id": one["id"], "title": "Patents"}
    )
    assert attached["outcomes"]["tertiary_outcome"]["study_id"] == one["id"]
    assert list(attached["outcomes"]) == ["tertiary_outcome"]
    world.post(
        path, {"outcome": "tertiary_outcome", "study_id": two["id"], "title": "Dup"}, status=409
    )
    world.post(
        path, {"outcome": "fourth_outcome", "study_id": one["id"], "title": "Dup"}, status=409
    )
    world.post(path, {"outcome": "Bad Id", "study_id": two["id"], "title": "Bad"}, status=422)
    free = world.post(path, {"outcome": "mixture_transfer", "study_id": two["id"], "title": "Free"})
    assert sorted(free["outcomes"]) == ["mixture_transfer", "tertiary_outcome"]
    two = study(world)
    world.post(
        path,
        {"outcome": "fourth_outcome", "study_id": two["id"], "title": "Agent"},
        headers=agent_headers(world),
        status=403,
    )
    world.post(
        path, {"outcome": "fourth_outcome", "study_id": "study_missing", "title": "x"}, status=404
    )
    other = world.post("/projects", {"name": "Other project"})
    foreign = study(world, other["id"])
    world.post(
        path, {"outcome": "fifth_outcome", "study_id": foreign["id"], "title": "x"}, status=404
    )
    world.post(
        "/research-programs/missing/outcomes",
        {"outcome": "fifth_outcome", "study_id": two["id"], "title": "x"},
        status=404,
    )
    assert (
        world.get(f"/research-programs/{made['id']}")["outcomes"]["tertiary_outcome"]["title"]
        == "Patents"
    )


def test_programs_are_project_scoped(tmp_path):
    world = GateWorld(tmp_path)
    made = program(world)
    other = world.post("/projects", {"name": "Other project"})
    scoped = agent_headers(world, other["id"])
    world.get(f"/research-programs/{made['id']}", status=404, headers=scoped)
    world.get(f"/projects/{world.project['id']}/research-programs", status=404, headers=scoped)
    assert world.get(f"/projects/{other['id']}/research-programs", headers=scoped) == {
        "research_programs": []
    }


def snapshot(world: GateWorld) -> dict:
    with world.db() as db:
        return {
            table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2")]
            for table in BUDGET_TABLES
        }


def authorize(world: GateWorld, experiment_id: str) -> dict:
    return world.post(
        f"/experiments/{experiment_id}/budget/authorization",
        {
            "expected_revision": 0,
            "ceiling_cents": 5000,
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "evidence_digest": "a" * 64,
            "source_reference": "synthetic-billing-snapshot",
            "prior_settled_cents": 0,
            "prior_pending_cents": 0,
            "ancillary_cents": 500,
            "shutdown_cents": 1000,
        },
    )


def charge(world, experiment_id, revision, category, amount, attempt=None) -> dict:
    return world.post(
        f"/experiments/{experiment_id}/budget/charges",
        {
            "expected_revision": revision,
            "delivery_id": uuid.uuid4().hex,
            "evidence_digest": "d" * 64,
            "source_reference": "synthetic-ledger",
            "category": category,
            "admitted_attempt_id": attempt,
            "amount_cents": amount,
        },
    )


def test_spend_rollup_is_read_only_and_totals(tmp_path):
    world = GateWorld(tmp_path)
    experiment_id = world.experiment["id"]
    authorize(world, experiment_id)
    attempt = world.admitted_attempt()
    with world.db() as db:
        db.execute("PRAGMA foreign_keys=OFF")
        actor = {"id": world.owner["id"], "org_id": world.owner["org_id"], "project_id": None}
        reserve_attempt(
            db,
            actor,
            {"id": attempt, "experiment_id": experiment_id},
            {"id": "grant_synthetic_roll", "max_exposure_cents": 300},
        )
    revision = world.get(f"/experiments/{experiment_id}/budget")["revision"]
    revision = charge(world, experiment_id, revision, "compute", 120, attempt)["revision"]
    charge(world, experiment_id, revision, "ancillary", 30)

    made = program(world)
    linked = study(world, experiment_id=experiment_id)
    empty = study(world)
    path = f"/research-programs/{made['id']}/outcomes"
    world.post(path, {"outcome": "tertiary_outcome", "study_id": linked["id"], "title": "Linked"})
    world.post(path, {"outcome": "sixth_outcome", "study_id": empty["id"], "title": "No runs"})

    before = snapshot(world)
    detail = world.get(f"/research-programs/{made['id']}")
    listed = world.get(f"/projects/{world.project['id']}/research-programs")["research_programs"]
    assert snapshot(world) == before and listed == [detail]

    linked_out = detail["outcomes"]["tertiary_outcome"]
    assert linked_out["experiments"] == [
        {
            "experiment_id": experiment_id,
            "budget_state": "active",
            "ceiling_cents": 5000,
            "recorded_cents": 150,
            "held_cents": 180,
            "settled_cents": 120,
        }
    ]
    assert linked_out["totals"]["recorded_cents"] == 150
    assert linked_out["totals"]["held_cents"] == 180
    assert linked_out["totals"]["settled_cents"] == 120
    assert detail["outcomes"]["sixth_outcome"]["experiments"] == []
    assert detail["outcomes"]["sixth_outcome"]["totals"]["recorded_cents"] == 0
    assert "fourth_outcome" not in detail["outcomes"]
    spend = detail["spend"]
    assert spend["spend_basis"] == "tessera_budget_ledger_records_not_provider_billing"
    assert spend["totals"]["recorded_cents"] == 150 and spend["totals"]["settled_cents"] == 120
    assert spend["experiments_without_budget"] == 0


def test_unbudgeted_and_foreign_experiments(tmp_path):
    world = GateWorld(tmp_path)
    made = program(world)
    linked = study(world, experiment_id=world.experiment["id"])
    outside = study(world, experiment_id="experiment_from_elsewhere")
    path = f"/research-programs/{made['id']}/outcomes"
    world.post(
        path, {"outcome": "seventh_outcome", "study_id": linked["id"], "title": "Unbudgeted"}
    )
    world.post(path, {"outcome": "eighth_outcome", "study_id": outside["id"], "title": "Unknown"})
    detail = world.get(f"/research-programs/{made['id']}")
    row = detail["outcomes"]["seventh_outcome"]["experiments"][0]
    assert row["budget_state"] == "no_budget" and row["ceiling_cents"] is None
    assert row["recorded_cents"] == row["held_cents"] == row["settled_cents"] == 0
    assert detail["outcomes"]["seventh_outcome"]["totals"]["experiments_without_budget"] == 1
    assert detail["outcomes"]["eighth_outcome"]["experiments"] == []
    assert detail["spend"]["experiments_without_budget"] == 1
