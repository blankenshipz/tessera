"""Isolated accounting tests: no credentials, provider effects or paid admission."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from tessera_control.api import create_app
from tessera_control.experiment_budget_models import (
    AuthorizeExperimentBudget,
    RecordExperimentCharge,
    ResolveExperimentBudgetBounds,
)
from tessera_control.experiment_budgets import (
    authorize_budget,
    budget_status,
    check_budget,
    create_experiment_budget_schema,
    record_charge,
    reserve_attempt,
    resolve_bounds,
)
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.store import Conflict, NotFound, initialize_state, migrate_state

ACTOR = {"id": "human", "org_id": "org", "project_id": None}


def authorization(**updates):
    data = dict(
        expected_revision=0,
        ceiling_cents=5000,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        evidence_digest="a" * 64,
        source_reference="synthetic-billing-snapshot",
        prior_settled_cents=0,
        prior_pending_cents=0,
        ancillary_cents=500,
        shutdown_cents=1000,
    )
    data.update(updates)
    return AuthorizeExperimentBudget.model_validate(data).model_dump(mode="json")


def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


@pytest.fixture
def ledger(tmp_path):
    path = tmp_path / "synthetic.db"
    with connect(path) as db:
        # Minimal synthetic native identities; real control schema tested separately.
        for sql in (
            "CREATE TABLE principals(id TEXT PRIMARY KEY)",
            "CREATE TABLE projects(id TEXT PRIMARY KEY, org_id TEXT)",
            "CREATE TABLE research_experiments(id TEXT PRIMARY KEY, project_id TEXT)",
            "CREATE TABLE execution_grants(id TEXT PRIMARY KEY)",
            "CREATE TABLE research_experiment_admitted_attempts"
            "(id TEXT PRIMARY KEY, experiment_id TEXT, state TEXT DEFAULT 'admitted')",
        ):
            db.execute(sql)
        db.execute("INSERT INTO principals VALUES ('human')")
        db.execute("INSERT INTO projects VALUES ('project','org')")
        db.execute("INSERT INTO research_experiments VALUES ('experiment','project')")
        create_experiment_budget_schema(db)
        authorize_budget(db, ACTOR, "experiment", authorization())
    return path


def reserve(db, ident, amount):
    db.execute("INSERT INTO execution_grants VALUES (?)", (ident,))
    db.execute(
        "INSERT INTO research_experiment_admitted_attempts(id,experiment_id) "
        "VALUES (?,'experiment')",
        (ident,),
    )
    reserve_attempt(
        db,
        ACTOR,
        {"id": ident, "experiment_id": "experiment"},
        {"id": ident, "max_exposure_cents": amount},
    )


@pytest.mark.parametrize("value", [True, -1, 5001, "5000", 5000.0])
def test_closed_strict_cap(value):
    with pytest.raises(ValidationError):
        authorization(ceiling_cents=value)


def test_concurrent_exact_limit_and_exceed_one_rollback(ledger):
    barrier = Barrier(2)

    def claim(ident):
        db = connect(ledger)
        barrier.wait()
        try:
            db.execute("BEGIN IMMEDIATE")
            reserve(db, ident, 3500)
            db.commit()
            return "reserved"
        except Conflict:
            db.rollback()
            return "rejected"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, ["a", "b"])) == ["rejected", "reserved"]
    with connect(ledger) as db:
        assert budget_status(db, ACTOR, "experiment")["remaining_cents"] == 0
        assert db.execute("SELECT count(*) FROM execution_grants").fetchone()[0] == 1
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment", 1)


def charge(revision, delivery, amount, **updates):
    data = dict(
        expected_revision=revision,
        delivery_id=delivery,
        amount_cents=amount,
        evidence_digest="b" * 64,
        source_reference="synthetic-invoice",
        category="compute",
        admitted_attempt_id="a",
    )
    data.update(updates)
    return RecordExperimentCharge.model_validate(data).model_dump(mode="json")


def test_restart_replay_partial_late_actual_overrun_is_recorded(ledger):
    with connect(ledger) as db:
        reserve(db, "a", 3500)
        first = charge(2, "delivery1", 1000)
        status = record_charge(db, ACTOR, "experiment", first)
        assert status["accounted_exposure_cents"] == 5000
    with connect(ledger) as db:
        assert record_charge(db, ACTOR, "experiment", first)["revision"] == 3
        with pytest.raises(Conflict):
            record_charge(db, ACTOR, "experiment", dict(first, amount_cents=1001))
        # Distinct real charge delivery, not amount-based deduplication.
        status = record_charge(db, ACTOR, "experiment", charge(3, "delivery2", 1000))
        assert status["revision"] == 4
        assert status["accounted_exposure_cents"] == 5000
        status = record_charge(db, ACTOR, "experiment", charge(4, "late", 6000))
        assert status["state"] == "fenced"
        assert status["accounted_exposure_cents"] == 9500
        settled = db.execute("SELECT settled_cents FROM experiment_budget_reservations").fetchone()[
            0
        ]
        assert settled == 8000
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment")


def test_tenant_project_expiry_unknown_and_reset_fail_closed(ledger):
    with connect(ledger) as db:
        for actor in (dict(ACTOR, org_id="other"), dict(ACTOR, project_id="other")):
            with pytest.raises(NotFound):
                check_budget(db, actor, "experiment")
        with pytest.raises(Conflict):
            authorize_budget(db, ACTOR, "experiment", authorization())
        db.execute("UPDATE experiment_budgets SET prior_pending_cents=NULL")
        assert budget_status(db, ACTOR, "experiment")["remaining_cents"] is None
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment")
        db.execute(
            "UPDATE experiment_budgets SET prior_pending_cents=0, expires_at='2000-01-01T00:00:00Z'"
        )
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment")


def test_authoritative_api_cas_replay_and_schema(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    headers = dict([state.joinpath("owner.header").read_text().strip().split(": ", 1)])
    with TestClient(create_app(state)) as client:

        def post(path, data, key):
            return client.post(path, json=data, headers=headers | {"Idempotency-Key": key})

        project = post("/projects", {"name": "budget"}, "project").json()["id"]
        variant = dict(name="synthetic", method_digest="a" * 64, input_digest="b" * 64, metadata={})
        response = post(
            f"/projects/{project}/experiments",
            dict(
                goal="synthetic",
                success_criterion="none",
                protocol="isolated",
                variants=[dict(variant, kind=kind) for kind in ("baseline", "intervention")],
            ),
            "experiment",
        )
        assert response.status_code == 201, response.text
        experiment = response.json()["id"]
        path = f"/experiments/{experiment}/budget/authorization"
        body = authorization(prior_pending_cents=None)
        response = post(path, body, "authorize")
        assert response.status_code == 200, response.text
        assert response.json()["exposure_unknown"]
        assert post(path, body, "authorize").json() == response.json()
        assert post(path, dict(body, ceiling_cents=4999), "authorize").status_code == 409
        assert post(path, body, "reset").status_code == 409
        assert post(path, dict(body, ceiling_cents=True), "invalid").status_code == 422
        assert client.get(f"/experiments/{experiment}/budget", headers=headers).status_code == 200


def test_native_admit_budget_failure_rolls_back_attempt_event_and_key_then_replays(
    tmp_path, monkeypatch
):
    from test_kubernetes_execution import _admitted_package

    from tessera_control.experiments import ExperimentStore
    from tessera_control.store import canonical_digest

    original = ExperimentStore._admit_attempt_transaction
    captured = {}

    def limited_admit(store, db, actor, variant_id, body):
        experiment = store._variant(db, actor, variant_id)["experiment_id"]
        captured.update(actor=actor, variant_id=variant_id, body=body, experiment=experiment)
        if not db.execute(
            "SELECT 1 FROM experiment_budgets WHERE experiment_id=?", (experiment,)
        ).fetchone():
            authorize_budget(
                db,
                actor,
                experiment,
                authorization(ceiling_cents=1, ancillary_cents=0, shutdown_cents=0),
            )
        return original(store, db, actor, variant_id, body)

    monkeypatch.setattr(ExperimentStore, "_admit_attempt_transaction", limited_admit)
    with pytest.raises(Conflict, match="insufficient"):
        _admitted_package(tmp_path)
    database = tmp_path / "state" / "tessera.sqlite3"
    with connect(database) as db:
        for table in (
            "experiment_budgets",
            "experiment_budget_reservations",
            "research_experiment_admitted_attempts",
            "research_experiment_admitted_attempt_states",
        ):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            db.execute(
                "SELECT count(*) FROM events WHERE event_type='experiment.attempt_admitted'"
            ).fetchone()[0]
            == 0
        )
        assert (
            db.execute(
                "SELECT count(*) FROM idempotency WHERE path LIKE '%/admitted-attempts'"
            ).fetchone()[0]
            == 0
        )
        native_hold = db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0]
    records = ExperimentStore(tmp_path / "state")
    records.budget_command(captured["actor"], captured["experiment"], "parent", authorization())
    args = (
        captured["actor"],
        captured["variant_id"],
        "native-admit-replay",
        canonical_digest(captured["body"]),
        captured["body"],
    )
    first = records.admit_attempt(*args)
    assert first[0] == 201
    assert records.admit_attempt(*args) == first
    with connect(database) as db:
        assert db.execute("SELECT count(*) FROM experiment_budget_reservations").fetchone()[0] == 1
        assert (
            db.execute("SELECT held_cents FROM execution_project_limits").fetchone()[0]
            == native_hold
        )


def _unbudgeted_experiment(tmp_path, states):
    path = tmp_path / "prior.db"
    with connect(path) as db:
        for sql in (
            "CREATE TABLE principals(id TEXT PRIMARY KEY)",
            "CREATE TABLE projects(id TEXT PRIMARY KEY, org_id TEXT)",
            "CREATE TABLE research_experiments(id TEXT PRIMARY KEY, project_id TEXT)",
            "CREATE TABLE execution_grants(id TEXT PRIMARY KEY)",
            "CREATE TABLE research_experiment_admitted_attempts"
            "(id TEXT PRIMARY KEY, experiment_id TEXT, state TEXT)",
        ):
            db.execute(sql)
        db.execute("INSERT INTO principals VALUES ('human')")
        db.execute("INSERT INTO projects VALUES ('project','org')")
        db.execute("INSERT INTO research_experiments VALUES ('experiment','project')")
        for index, state in enumerate(states):
            db.execute(
                "INSERT INTO research_experiment_admitted_attempts VALUES (?,?,?)",
                (f"prior-{index}", "experiment", state),
            )
        create_experiment_budget_schema(db)
    return path


def test_terminal_prior_attempts_allow_budget_with_declared_history(tmp_path):
    path = _unbudgeted_experiment(tmp_path, ["succeeded", "failed"])
    with connect(path) as db:
        status = authorize_budget(
            db, ACTOR, "experiment", authorization(prior_settled_cents=12, prior_pending_cents=0)
        )
        assert status["exposure_unknown"] is False
        assert status["accounted_exposure_cents"] == 12 + 500 + 1000
        reserve(db, "gpu", 300)
        assert check_budget(db, ACTOR, "experiment")["accounted_exposure_cents"] == 1812


@pytest.mark.parametrize(
    ("states", "updates"),
    [
        (["succeeded", "running"], {}),
        (["succeeded"], {"prior_settled_cents": None}),
        (["cancelled"], {"prior_pending_cents": None}),
    ],
)
def test_prior_attempts_require_terminal_state_and_known_history(tmp_path, states, updates):
    path = _unbudgeted_experiment(tmp_path, states)
    with connect(path) as db, pytest.raises(Conflict):
        authorize_budget(db, ACTOR, "experiment", authorization(**updates))


def bounds(revision=1, **updates):
    data = dict(
        expected_revision=revision,
        evidence_digest="c" * 64,
        source_reference="synthetic-resolution",
    )
    data.update(updates)
    return ResolveExperimentBudgetBounds.model_validate(data).model_dump(mode="json")


@pytest.fixture
def unknown_ledger(ledger):
    with connect(ledger) as db:
        db.execute("UPDATE experiment_budgets SET prior_pending_cents=NULL, shutdown_cents=NULL")
    return ledger


def test_resolve_unknown_bound_unblocks_admission_and_records_evidence(unknown_ledger):
    with connect(unknown_ledger) as db:
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment")
        status = resolve_bounds(db, ACTOR, "experiment", bounds(prior_pending_cents=10))
        assert status["exposure_unknown"] and status["revision"] == 2
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment")
        status = resolve_bounds(db, ACTOR, "experiment", bounds(2, shutdown_cents=1000))
        assert not status["exposure_unknown"] and status["revision"] == 3
        assert status["accounted_exposure_cents"] == 500 + 10 + 1000
        check_budget(db, ACTOR, "experiment", 1)
        rows = db.execute(
            "SELECT field,amount_cents,revision,actor_principal_id FROM "
            "experiment_budget_bound_resolutions ORDER BY revision"
        ).fetchall()
        assert [tuple(r) for r in rows] == [
            ("prior_pending_cents", 10, 2, "human"),
            ("shutdown_cents", 1000, 3, "human"),
        ]
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM experiment_budget_bound_resolutions")


def test_resolve_rejects_known_overwrite_stale_revision_and_empty(unknown_ledger):
    with connect(unknown_ledger) as db:
        with pytest.raises(Conflict):
            resolve_bounds(db, ACTOR, "experiment", bounds(ancillary_cents=0))
        with pytest.raises(Conflict):
            resolve_bounds(
                db, ACTOR, "experiment", bounds(prior_pending_cents=1, ancillary_cents=0)
            )
        with pytest.raises(Conflict):
            resolve_bounds(db, ACTOR, "experiment", bounds(2, prior_pending_cents=1))
        row = db.execute(
            "SELECT revision,prior_pending_cents,ancillary_cents FROM experiment_budgets"
        ).fetchone()
        assert tuple(row) == (1, None, 500)
        assert (
            db.execute("SELECT count(*) FROM experiment_budget_bound_resolutions").fetchone()[0]
            == 0
        )
        resolve_bounds(db, ACTOR, "experiment", bounds(prior_pending_cents=1))
        with pytest.raises(Conflict):  # once resolved it is known and cannot be lowered
            resolve_bounds(db, ACTOR, "experiment", bounds(2, prior_pending_cents=0))
    for bad in (
        dict(),
        dict(prior_pending_cents=-1),
        dict(prior_pending_cents=True),
        dict(prior_pending_cents=1, expected_revision=0),
        dict(prior_pending_cents=1, evidence_digest="x"),
        dict(prior_pending_cents=1, extra=1),
    ):
        with pytest.raises(ValidationError):
            bounds(**bad)


def test_resolve_breaching_ceiling_fences_parent(unknown_ledger):
    with connect(unknown_ledger) as db:
        status = resolve_bounds(db, ACTOR, "experiment", bounds(prior_pending_cents=4000))
        assert status["state"] == "active" and status["exposure_unknown"]
        status = resolve_bounds(db, ACTOR, "experiment", bounds(2, shutdown_cents=1000))
        assert status["state"] == "fenced"
        assert status["accounted_exposure_cents"] == 5500 > status["ceiling_cents"]
        with pytest.raises(Conflict):
            check_budget(db, ACTOR, "experiment")


def test_bounds_api_replay_human_only_and_conflicts(tmp_path):
    state = tmp_path / "state"
    initialize_state(state)
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    headers = dict([state.joinpath("owner.header").read_text().strip().split(": ", 1)])
    with TestClient(create_app(state)) as client:

        def post(path, data, key, hdrs=headers):
            return client.post(path, json=data, headers=hdrs | {"Idempotency-Key": key})

        project = post("/projects", {"name": "budget"}, "project").json()["id"]
        variant = dict(name="synthetic", method_digest="a" * 64, input_digest="b" * 64, metadata={})
        experiment = post(
            f"/projects/{project}/experiments",
            dict(
                goal="synthetic",
                success_criterion="none",
                protocol="isolated",
                variants=[dict(variant, kind=kind) for kind in ("baseline", "intervention")],
            ),
            "experiment",
        ).json()["id"]
        base = f"/experiments/{experiment}/budget"
        auth = post(
            f"{base}/authorization",
            authorization(prior_pending_cents=None, shutdown_cents=None),
            "authorize",
        )
        assert auth.status_code == 200 and auth.json()["exposure_unknown"]
        path = f"{base}/bounds"
        body = bounds(prior_pending_cents=0, shutdown_cents=1000)
        first = post(path, body, "resolve")
        assert first.status_code == 200, first.text
        assert not first.json()["exposure_unknown"] and first.json()["revision"] == 2
        assert post(path, body, "resolve").json() == first.json()
        assert post(path, dict(body, shutdown_cents=1), "resolve").status_code == 409
        assert post(path, body, "again").status_code == 409  # stale revision, known bounds
        assert post(path, bounds(2, ancillary_cents=1), "known").status_code == 409
        assert (
            post(
                path,
                dict(expected_revision=2, evidence_digest="c" * 64, source_reference="x"),
                "empty",
            ).status_code
            == 422
        )
        events = client.get(f"/experiments/{experiment}/budget", headers=headers).json()
        assert events["revision"] == 2


def test_resolve_refused_for_non_human_actor(tmp_path, monkeypatch):
    from tessera_control.experiments import ExperimentStore
    from tessera_control.store import Forbidden

    state = tmp_path / "state"
    initialize_state(state)
    store = ExperimentStore(state)
    monkeypatch.setattr(store, "_live_principal", lambda c, p, **kw: dict(ACTOR, role="agent"))
    with pytest.raises(Forbidden):
        store.budget_command({}, "experiment", "key", bounds(prior_pending_cents=1), bounds=True)


def test_database_upgraded_from_v57_gets_bound_resolution_table_and_backs_up(tmp_path):
    from tessera_control.recovery import backup_state

    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        # A v57-era database created before bound resolutions existed.
        for event in ("update", "delete"):
            db.execute(f"DROP TRIGGER experiment_budget_bound_resolutions_no_{event}")
        db.execute("DROP TABLE experiment_budget_bound_resolutions")
        db.execute("UPDATE schema_meta SET schema_version=58")
    assert migrate_state(state) == LATEST_SCHEMA_VERSION
    with sqlite3.connect(state / "tessera.sqlite3") as db:
        assert db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='experiment_budget_bound_resolutions'"
        ).fetchone()
    assert backup_state(state, tmp_path / "backup")["schema_version"] == LATEST_SCHEMA_VERSION
