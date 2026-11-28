"""deploy-upgrade refuses a stop that would lapse live execution leases, and runs each
service's startup check in the preflight. Fakes only: no service is touched."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from test_deploy_upgrade import Fakes, make_plan
from test_deploy_upgrade import run as run_with_fakes

from tessera_control import deploy_upgrade, focused_tests, startup_checks
from tessera_control.deploy_check import Finding
from tessera_control.deploy_upgrade_plan import PlanError, load_plan
from tessera_control.store import DATABASE_NAME

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def stamp(seconds):
    return (NOW + timedelta(seconds=seconds)).isoformat().replace("+00:00", "Z")


def seed(db, table, values):
    """Insert one row, filling every other NOT NULL column with a dummy (foreign keys off)."""
    columns = db.execute(f"PRAGMA table_info({table})").fetchall()
    row = dict(values)
    for _cid, name, kind, notnull, default, _pk in columns:
        if name not in row and notnull and default is None:
            row[name] = 1 if kind.upper() == "INTEGER" else "x"
    marks = ",".join("?" for _ in row)
    db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES ({marks})", tuple(row.values()))


def add_lease(state, expires_in, *, phase="route_started", n=1, grant_status="active"):
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("PRAGMA foreign_keys=OFF")
        seed(
            db,
            "execution_grants",
            {"id": f"g{n}", "task_id": f"t{n}", "status": grant_status, "currency": "USD"},
        )
        seed(
            db,
            "execution_leases",
            {"id": f"l{n}", "grant_id": f"g{n}", "expires_at": stamp(expires_in)},
        )
        seed(
            db,
            "campaign_salad_attempts",
            {
                "attempt_id": f"a{n}",
                "phase": phase,
                "lease_id": f"l{n}",
                "route_id": f"r{n}",
                "revision": 1,
                "updated_at": "2026-01-01T00:00:00Z",
            },
        )


def run(plan, fakes, **extra):
    return run_with_fakes(plan, fakes, now=lambda: NOW, **extra)


def blocking(document, prefix="salad."):
    return [e for e in document["preflight"]["blocking"] if e.startswith(prefix)]


def test_a_lease_expiring_inside_the_downtime_blocks_and_nothing_is_stopped(tmp_path):
    plan, _p, state = make_plan(tmp_path)
    add_lease(state, 100)
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert plan.expected_downtime_seconds == 120
    assert document["failed_step"] == "preflight"
    assert blocking(document)[0].startswith("salad.lease_lapse (1 ")
    assert "150 s or more" in blocking(document)[0]
    assert fakes.calls == []
    assert document["salad_lease_holding"] == 1 and document["salad_leases_lapsing"] == 1
    assert document["salad_route_holding"] == 1 and document["salad_executing"] == 0


def test_the_allow_flag_makes_it_a_warning_and_the_run_proceeds(tmp_path):
    plan, _p, state = make_plan(tmp_path)
    add_lease(state, 100)
    document = run(plan, Fakes(), execute=False, allow_lease_lapse=True)
    assert document["result"] == "ok" and blocking(document) == []
    assert any("will lapse" in w for w in document["warnings"])
    fakes = Fakes()
    document = run(plan, fakes, execute=True, allow_lease_lapse=True)
    assert document["result"] == "ok", document["steps"]
    assert ["stop-executor"] in fakes.calls


def test_a_lease_that_outlives_the_downtime_does_not_block(tmp_path):
    plan, _p, state = make_plan(tmp_path)
    add_lease(state, 600)
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert document["result"] == "ok", document["steps"]
    assert document["salad_lease_holding"] == 1 and document["salad_leases_lapsing"] == 0


def test_expired_or_released_grant_leases_are_not_held(tmp_path):
    plan, _p, state = make_plan(tmp_path)
    add_lease(state, -10, n=1)
    add_lease(state, 50, n=2, grant_status="reconciled")
    add_lease(state, 50, n=3, phase="outcome_recorded")
    document = run(plan, Fakes(), execute=False)
    assert document["result"] == "ok"
    assert document["salad_lease_holding"] == 0


def test_a_lease_appearing_before_the_stop_is_caught_by_the_recheck(tmp_path, monkeypatch):
    plan, path, state = make_plan(tmp_path)
    root = tmp_path / "repo"
    (root / "src" / "pkg").mkdir(parents=True)
    (root / "tests").mkdir()
    for name in ("test_alpha.py", "test_beta.py", "test_smoke.py"):
        (root / "tests" / name).write_text("def test_x():\n    pass\n")
    monkeypatch.setattr(focused_tests, "changed_files", lambda *a, **k: ["tests/test_alpha.py"])
    monkeypatch.setattr(deploy_upgrade, "checkout_head", lambda where: "a" * 40)
    raw = json.loads(path.read_text())
    raw.update(focused_tests=True, from_revision="b" * 40, repo_root=str(root))
    path.write_text(json.dumps(raw))
    plan = load_plan(path)
    ran = []

    def focused(files, root, jobs):
        ran.append(files)
        add_lease(state, 100)  # taken while the focused tests run
        return 0

    fakes = Fakes()
    document = run(plan, fakes, execute=True, focused_runner=focused)
    assert ran, document["steps"]
    assert document["result"] == "failed"
    assert document["failed_step"] == "preflight:lease_recheck"
    assert fakes.calls == []
    assert "salad.lease_lapse" in document["steps"][-1]["detail"]


def test_an_executing_attempt_still_blocks(tmp_path):
    plan, _p, state = make_plan(tmp_path)
    add_lease(state, 600, phase="executing")
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert blocking(document)[0].startswith("salad.executing (1 ")
    assert fakes.calls == []
    assert document["salad_route_holding"] == 1


def test_an_unreadable_database_blocks_but_a_missing_one_is_zero(tmp_path):
    plan, _p, state = make_plan(tmp_path)
    snap = deploy_upgrade.salad_lease_snapshot(tmp_path / "missing", now=NOW, downtime_seconds=120)
    assert snap == deploy_upgrade.LeaseSnapshot()
    (state / DATABASE_NAME).write_bytes(b"not a database" * 100)
    snap = deploy_upgrade.salad_lease_snapshot(state, now=NOW, downtime_seconds=120)
    assert snap.unreadable
    _snap, blocked, _w = deploy_upgrade.lease_guard(
        plan, NOW, allow_executing_salad=True, allow_lease_lapse=True
    )
    assert blocked and blocked[0].startswith("salad.lease_state")


def test_a_database_without_salad_tables_is_no_data_with_a_note(tmp_path):
    state = tmp_path / "s"
    state.mkdir()
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("CREATE TABLE unrelated(x)")
    snap = deploy_upgrade.salad_lease_snapshot(state, now=NOW, downtime_seconds=120)
    assert not snap.unreadable and snap.lease_holding == 0 and snap.notes


def _with_config_check(path, checks):
    raw = json.loads(path.read_text())
    for service in raw["services"]:
        if service["name"] in checks:
            service["config_check"] = checks[service["name"]]
    raw["expected_downtime_seconds"] = 300
    path.write_text(json.dumps(raw))
    return load_plan(path)


def test_config_check_reaches_the_startup_checks_and_a_failure_blocks_before_a_stop(
    tmp_path, monkeypatch
):
    _plan, path, _state = make_plan(tmp_path)
    plan = _with_config_check(
        path, {"executor": {"program": "salad-executor", "args": ["--config", "/etc/x.json"]}}
    )
    assert plan.expected_downtime_seconds == 300
    seen = []

    def fake_check_all(checks, **_):
        seen.append(list(checks))
        return [Finding("startup.salad-executor.1", "startup", "fail", "refused", {})]

    monkeypatch.setattr(startup_checks, "check_all", fake_check_all)
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert seen[0] == [startup_checks.StartupCheck("salad-executor", ("--config", "/etc/x.json"))]
    assert "startup.salad-executor.1" in document["preflight"]["blocking"]
    assert document["failed_step"] == "preflight" and fakes.calls == []


def test_the_post_install_smoke_runs_the_same_startup_checks(tmp_path, monkeypatch):
    _plan, path, _state = make_plan(tmp_path)
    plan = _with_config_check(path, {"control": {"program": "serve", "args": []}})
    calls = []

    def fake_check_all(checks, **_):
        calls.append(len(list(checks)))
        return []

    monkeypatch.setattr(startup_checks, "check_all", fake_check_all)
    document = run(plan, Fakes(), execute=True)
    assert document["result"] == "ok", document["steps"]
    assert calls == [1, 1]  # preflight and the post-install smoke


@pytest.mark.parametrize(
    "check",
    [
        {"program": "other", "args": []},
        {"program": "serve", "args": ["--check-config"]},
        {"program": "serve", "args": ["a\0b"]},
        {"program": "serve", "args": ["x"] * 65},
        {"program": "serve", "args": ["x" * 513]},
        {"program": "serve", "extra": 1},
    ],
)
def test_a_malformed_config_check_is_refused(tmp_path, check):
    _plan, path, _state = make_plan(tmp_path)
    raw = json.loads(path.read_text())
    raw["services"][0]["config_check"] = check
    path.write_text(json.dumps(raw))
    with pytest.raises(PlanError):
        load_plan(path)


def test_the_downtime_bounds(tmp_path):
    _plan, path, _state = make_plan(tmp_path)
    raw = json.loads(path.read_text())
    for bad in (4, 3601):
        raw["expected_downtime_seconds"] = bad
        path.write_text(json.dumps(raw))
        with pytest.raises(PlanError):
            load_plan(path)
