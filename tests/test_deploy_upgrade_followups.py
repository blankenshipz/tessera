"""Deploy-upgrade correctness follow-ups: config order, unstopped services, health freshness,
recovery with a pending migration, rollback, and failure details without paths (ADR 0099)."""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_deploy_config import CANCEL
from test_deploy_upgrade import Fakes, make_plan, run

from tessera_control import deploy_services as ds
from tessera_control import deploy_upgrade
from tessera_control.cli import main
from tessera_control.deploy_upgrade_plan import load_plan
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.store import DATABASE_NAME


def _config(plan):
    return plan.live_configs[0]


def _set_schema(state, value):
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("UPDATE schema_meta SET schema_version=?", (value,))


# (1) config fixes after migrate ---------------------------------------------------------------


def test_a_failed_migrate_leaves_the_configs_with_the_old_key(tmp_path, monkeypatch):
    plan, _path, _state = make_plan(tmp_path)
    before = open(_config(plan)).read()

    def boom(*_a, **_k):
        raise RuntimeError("migration broke")

    monkeypatch.setattr("tessera_control.deploy_state.apply_migrate", boom)
    document = run(plan, Fakes(), execute=True)
    assert document["failed_step"] == "migrate"
    assert open(_config(plan)).read() == before and CANCEL in before


def test_rollback_restores_the_snapshot_and_the_old_config_key(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    document = run(plan, Fakes(), execute=True)
    assert document["result"] == "ok"
    assert CANCEL not in open(_config(plan)).read()  # the fix stripped it
    receipt = deploy_upgrade.write_receipt(plan, document)
    healthy = lambda _h, **_: (False, "down")  # noqa: E731
    result = deploy_upgrade.rollback(plan, receipt, probe=healthy)
    assert result["result"] == "ok" and result["configs_restored"] == [0]
    assert CANCEL in open(_config(plan)).read()  # the old key is back
    with sqlite3.connect(state / DATABASE_NAME) as db:
        version = db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
    assert version == LATEST_SCHEMA_VERSION - 1
    assert (state / (DATABASE_NAME + ".pre-rollback")).exists()
    assert list((tmp_path / "receipts").glob("rollback-*.json"))


# (2) services that are not stopped -----------------------------------------------------------


def _without_stop(path, declare_read_only):
    document = json.loads(path.read_text())
    service = document["services"][0]
    service["stop_argv"] = None
    if declare_read_only:
        service["writes_state"] = False
    path.write_text(json.dumps(document))
    return load_plan(path)


def test_execute_refuses_an_unstopped_service_unless_declared_read_only(tmp_path):
    plan, path, _state = make_plan(tmp_path)
    refused = _without_stop(path, declare_read_only=False)
    fakes = Fakes()
    document = run(refused, fakes, execute=True)
    assert document["failed_step"] == "preflight" and fakes.calls == []
    assert "no stop_argv" in " ".join(document["preflight"]["blocking"])
    allowed = _without_stop(path, declare_read_only=True)
    document = run(allowed, Fakes(), execute=True)
    assert document["result"] == "ok"
    assert document["not_stopped"] == ["control"]


def test_the_dry_run_names_the_services_it_would_not_stop(tmp_path):
    plan, path, _state = make_plan(tmp_path)
    document = run(_without_stop(path, declare_read_only=False), Fakes(), execute=False)
    assert document["result"] == "ok" and document["not_stopped"] == ["control"]
    text = " | ".join(document["planned"])
    assert "NOT stopped (no stop_argv): control" in text and "execute refuses" in text


# (3) health file freshness ---------------------------------------------------------------------


def test_an_old_fresh_health_file_does_not_count_after_a_restart(tmp_path):
    path = tmp_path / "health"
    path.write_text(
        json.dumps({"schema": ds.HEALTH_SCHEMA, "state": "running", "last_loop_at": 1000.0})
    )
    health = SimpleNamespace(kind="file", path=str(path), max_age_seconds=60)
    assert ds.probe_health(health, now=lambda: 1010.0)[0]  # fresh to a plain probe
    ok, reason = ds.probe_health(health, now=lambda: 1010.0, not_before=1005.0)
    assert not ok and "predates the restart" in reason
    assert ds.probe_health(health, now=lambda: 1010.0, not_before=1000.0)[0]


def test_start_services_hands_the_restart_time_to_the_probe():
    seen = []

    def probe(health, *, not_before=None):
        seen.append(not_before)
        return True, "healthy"

    service = SimpleNamespace(
        name="a", restart_argv=["r"], stop_argv=["s"], health=None,
        health_wait_seconds=5, command_timeout_seconds=5,
    )  # fmt: skip
    ds.start_services([service], lambda a, t: (0, ""), probe=probe, wall=lambda: 42.0)
    assert seen == [42.0]


# (4) recovery with a pending migration ----------------------------------------------------------


def test_a_failed_backup_with_a_pending_migration_does_not_restart_the_new_release(tmp_path):
    plan, _path, _state = make_plan(tmp_path)
    fakes = Fakes()

    def broken(*_a, **_k):
        raise ValueError("no space")

    document = run(plan, fakes, execute=True, backup=broken)
    assert not any(call[0].startswith("restart-") for call in fakes.calls)
    last = document["steps"][-1]
    assert last["step"] == "restore-services" and not last["ok"]
    assert "migration is pending" in last["detail"]


def test_a_failed_backup_with_nothing_pending_still_restarts_the_services(tmp_path):
    plan, _path, _state = make_plan(tmp_path, behind=False)
    fakes = Fakes()

    def broken(*_a, **_k):
        raise ValueError("no space")

    run(plan, fakes, execute=True, backup=broken)
    assert ["restart-control"] in fakes.calls


# (5) rollback guards ------------------------------------------------------------------------------


def _executed(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    document = run(plan, Fakes(), execute=True)
    return plan, deploy_upgrade.write_receipt(plan, document), state


def test_rollback_refuses_while_a_service_is_healthy(tmp_path):
    plan, receipt, state = _executed(tmp_path)
    before = (state / DATABASE_NAME).read_bytes()
    with pytest.raises(deploy_upgrade.RollbackRefused, match="healthy"):
        deploy_upgrade.rollback(plan, receipt, probe=lambda _h, **_: (True, "healthy"))
    assert (state / DATABASE_NAME).read_bytes() == before


def test_rollback_refuses_a_tampered_receipt_or_snapshot(tmp_path):
    plan, receipt, state = _executed(tmp_path)
    down = lambda _h, **_: (False, "down")  # noqa: E731
    snapshot = next((tmp_path / "backups").glob("backup-*/snapshot.sqlite3"))
    snapshot.write_bytes(snapshot.read_bytes() + b"x")
    with pytest.raises(deploy_upgrade.RollbackRefused, match="digest"):
        deploy_upgrade.rollback(plan, receipt, probe=down)
    document = json.loads(receipt.read_text())
    document["result"] = "tampered"
    receipt.write_text(json.dumps(document))
    with pytest.raises(deploy_upgrade.RollbackRefused, match="receipt digest"):
        deploy_upgrade.rollback(plan, receipt, probe=down)


def test_rollback_command_reports_a_refusal_without_a_path(tmp_path, capsys, monkeypatch):
    plan, receipt, _state = _executed(tmp_path)
    plan_path = tmp_path / "plan" / "plan.json"
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "deploy-rollback", "--plan", str(plan_path), "--receipt", str(receipt)],
    )
    # The fake services have no health file, so none is healthy; the database is idle.
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["result"] == "ok"
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "deploy-rollback", "--plan", str(plan_path), "--receipt", str(tmp_path)],
    )
    assert main() == 2
    assert str(tmp_path) not in capsys.readouterr().err


# (6) failure details carry no path


def test_failure_steps_record_the_exception_type_and_no_path(tmp_path):
    plan, _path, _state = make_plan(tmp_path)
    secret_path = str(tmp_path / "private" / "location")

    def broken(*_a, **_k):
        raise ValueError(f"cannot write {secret_path}")

    document = run(plan, Fakes(), execute=True, backup=broken)
    detail = next(s["detail"] for s in document["steps"] if s["step"] == "backup")
    assert detail == "backup_failed (ValueError)"
    assert secret_path not in json.dumps(document) and str(tmp_path) not in json.dumps(
        document["steps"]
    )
