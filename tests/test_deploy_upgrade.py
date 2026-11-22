"""``tessera deploy-upgrade``: dry run by default, ordered execute with a receipt (ADR 0099)."""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_deploy_config import CANCEL, LIVE_CONFIG_KEYS, LIVE_CONFIG_SCHEMA, write

from tessera_control import deploy_upgrade
from tessera_control.cli import main
from tessera_control.deploy_upgrade_plan import PLAN_SCHEMA, load_plan
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.store import DATABASE_NAME, initialize_state


class Fakes:
    """A recording runner and health probe in place of real services."""

    def __init__(self, *, fail_restart: str | None = None):
        self.calls: list[list[str]] = []
        self.fail_restart = fail_restart

    def runner(self, argv, timeout):
        self.calls.append(list(argv))
        if self.fail_restart and argv[:1] == ["restart-" + self.fail_restart]:
            return 1, "boom"
        return 0, "ok"

    def probe(self, health, **_):
        return True, "healthy"


def make_plan(
    tmp_path, *, behind=True, with_config=True, smoke=False, services=("control", "executor")
):
    state = tmp_path / "state"
    initialize_state(state)
    if behind:
        with sqlite3.connect(state / DATABASE_NAME) as db:
            db.execute("UPDATE schema_meta SET schema_version=?", (LATEST_SCHEMA_VERSION - 1,))
    for name in ("backups", "receipts", "health"):
        (tmp_path / name).mkdir(mode=0o700)
    live = []
    if with_config:
        document = {key: "x" for key in LIVE_CONFIG_KEYS} | {
            "schema": LIVE_CONFIG_SCHEMA,
            CANCEL: "y",
        }
        cfg = tmp_path / "cfg"
        cfg.mkdir(mode=0o700)
        live = [str(write(cfg, document))]
    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(mode=0o700)
    plan = {
        "schema": PLAN_SCHEMA,
        "state_dir": str(state),
        "backup_dir": str(tmp_path / "backups"),
        "receipt_dir": str(tmp_path / "receipts"),
        "source_revision": "a" * 40,
        "live_configs": live,
        "smoke_cpu": smoke,
        "services": [
            {
                "name": name,
                "stop_argv": ["stop-" + name],
                "restart_argv": ["restart-" + name],
                "health": {
                    "kind": "file",
                    "path": str(tmp_path / "health" / name),
                    "max_age_seconds": 60,
                },
            }
            for name in services
        ],
    }
    path = plan_dir / "plan.json"
    path.write_text(json.dumps(plan))
    path.chmod(0o600)
    return load_plan(path), path, state


def run(plan, fakes, **extra):
    return deploy_upgrade.run_upgrade(
        plan,
        runner=fakes.runner,
        probe=fakes.probe,
        sleep=lambda _s: None,
        **extra,
    )


def steps(document):
    return [(s["step"], s["ok"]) for s in document["steps"]]


def test_a_dry_run_reports_the_plan_and_changes_nothing(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    fakes = Fakes()
    before = (state / DATABASE_NAME).read_bytes()
    document = run(plan, fakes, execute=False)
    assert document["mode"] == "dry-run" and document["result"] == "ok"
    assert fakes.calls == []  # no service was touched
    assert (state / DATABASE_NAME).read_bytes() == before
    assert list((tmp_path / "backups").iterdir()) == []
    assert list((tmp_path / "receipts").iterdir()) == []
    planned = " | ".join(document["planned"])
    assert "stop: executor, control" in planned and "migrate schema" in planned
    assert "drop removed config keys" in planned
    assert document["schema_version"]["before"] == LATEST_SCHEMA_VERSION - 1


def test_execute_runs_the_steps_in_order_and_fixes_everything(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert document["result"] == "ok", document["steps"]
    order = [s["step"] for s in document["steps"]]
    assert order == [
        "preflight", "stop:executor", "stop:control", "backup", "config-fixes", "migrate",
        "restart:control", "health:control", "restart:executor", "health:executor", "smoke",
    ]  # fmt: skip
    # Stopped last to first, restarted first to last.
    assert fakes.calls == [
        ["stop-executor"],
        ["stop-control"],
        ["restart-control"],
        ["restart-executor"],
    ]
    assert document["schema_version"] == {
        "before": LATEST_SCHEMA_VERSION - 1,
        "latest": LATEST_SCHEMA_VERSION,
        "after": LATEST_SCHEMA_VERSION,
    }
    assert len(list((tmp_path / "backups").iterdir())) == 1
    assert sorted(f["fix"] for f in document["fixes"]) == ["drop_removed_keys", "migrate"]
    assert document["source_revision"] == "a" * 40
    assert document["duration_seconds"] >= 0 and document["started_at"].endswith("Z")


def test_the_receipt_is_private_and_names_no_path_or_value(tmp_path):
    plan, _path, _state = make_plan(tmp_path)
    document = run(plan, Fakes(), execute=True)
    written = deploy_upgrade.write_receipt(plan, document)
    assert oct(written.stat().st_mode & 0o777) == "0o600"
    text = written.read_text()
    assert str(tmp_path) not in text
    assert json.loads(text)["receipt_sha256"]


def test_a_blocking_preflight_stops_before_any_service_is_touched(tmp_path):
    plan, _path, state = make_plan(tmp_path, with_config=False)
    (state / DATABASE_NAME).unlink()  # the schema check cannot be fixed by this command
    fakes = Fakes()
    document = run(plan, fakes, execute=True)
    assert document["result"] == "failed" and document["failed_step"] == "preflight"
    assert fakes.calls == []
    assert document["preflight"]["blocking"]


def test_a_failed_backup_restores_the_stopped_services_and_changes_nothing(tmp_path):
    plan, _path, state = make_plan(tmp_path)
    fakes = Fakes()

    def broken_backup(*_a, **_k):
        raise ValueError("size bound exceeded")

    document = run(plan, fakes, execute=True, backup=broken_backup)
    assert document["failed_step"] == "backup"
    assert fakes.calls[-2:] == [["restart-control"], ["restart-executor"]]
    assert document["steps"][-1]["step"] == "restore-services"
    with sqlite3.connect(state / DATABASE_NAME) as db:
        assert (
            db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION - 1
        )


def test_a_service_that_will_not_restart_stops_the_run_and_the_receipt_says_so(tmp_path):
    plan, _path, _state = make_plan(tmp_path)
    fakes = Fakes(fail_restart="control")
    document = run(plan, fakes, execute=True)
    assert document["result"] == "failed"
    assert document["failed_step"] == "restart:control"
    assert ["restart-executor"] not in fakes.calls  # later services are not touched
    assert document["backup"]["name"].startswith("backup-")


def test_a_failed_stop_aborts_before_the_state_is_copied(tmp_path):
    plan, _path, _state = make_plan(tmp_path)
    fakes = Fakes()
    original = fakes.runner

    def stop_fails(argv, timeout):
        fakes.calls.append(list(argv))
        return (1, "busy") if argv == ["stop-executor"] else (0, "ok")

    fakes.runner = stop_fails
    document = run(plan, fakes, execute=True)
    assert document["failed_step"] == "stop:executor"
    assert document["backup"] is None
    assert original is not None


def test_nothing_pending_still_runs_the_other_steps_and_applies_no_migration(tmp_path):
    plan, _path, _state = make_plan(tmp_path, behind=False, with_config=False)
    document = run(plan, Fakes(), execute=True)
    assert document["result"] == "ok"
    assert document["fixes"] == []
    assert document["schema_version"]["after"] == LATEST_SCHEMA_VERSION


# -- the command ---------------------------------------------------------------------------------


def test_the_command_defaults_to_a_dry_run_and_execute_writes_the_receipt(
    tmp_path, capsys, monkeypatch
):
    plan, path, state = make_plan(tmp_path, with_config=False)
    monkeypatch.setattr(
        "sys.argv", ["tessera", "deploy-upgrade", "--plan", str(path), "--format", "json"]
    )
    assert main() == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    assert list((tmp_path / "receipts").iterdir()) == []
    with sqlite3.connect(state / DATABASE_NAME) as db:
        assert (
            db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION - 1
        )


def test_a_bad_plan_is_a_usage_error_and_never_echoes_it(tmp_path, capsys, monkeypatch):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "nope", "secret_sentinel": "do-not-echo"}))
    bad.chmod(0o600)
    monkeypatch.setattr("sys.argv", ["tessera", "deploy-upgrade", "--plan", str(bad)])
    assert main() == 2
    assert "do-not-echo" not in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--execute"])
def test_execute_is_a_flag_not_a_default(flag):
    from argparse import ArgumentParser

    from tessera_control.deploy_upgrade_cli import add_parser

    parser = ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["deploy-upgrade", "--plan", "/x"]).execute is False
    assert parser.parse_args(["deploy-upgrade", "--plan", "/x", flag]).execute is True


# -- the backup this command takes --------------------------------------------------------------


def test_an_old_schema_database_gets_a_digested_pre_migration_snapshot(tmp_path):
    import hashlib

    _plan, _path, state = make_plan(tmp_path)
    out = tmp_path / "backups" / "snap"
    manifest = deploy_upgrade.take_backup(state, out, max_bytes=None)
    assert manifest["kind"] == "pre-migration-snapshot"
    assert manifest["schema_version"] == LATEST_SCHEMA_VERSION - 1
    snapshot = out / deploy_upgrade.SNAPSHOT_NAME
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == manifest["sha256"]
    with sqlite3.connect(snapshot) as db:
        assert db.execute("SELECT schema_version FROM schema_meta").fetchone()[0] == (
            LATEST_SCHEMA_VERSION - 1
        )
    assert oct(snapshot.stat().st_mode & 0o777) == "0o600"
    assert oct(out.stat().st_mode & 0o777) == "0o700"
    assert json.loads((out / deploy_upgrade.SNAPSHOT_MANIFEST).read_text()) == manifest


def test_a_current_database_gets_the_validated_backup(tmp_path):
    _plan, _path, state = make_plan(tmp_path, behind=False)
    out = tmp_path / "backups" / "valid"
    assert deploy_upgrade.take_backup(state, out)["kind"] == "validated"
    assert (out / "manifest.json").is_file()


def test_the_snapshot_refuses_a_database_over_the_bound_and_an_existing_output(
    tmp_path, monkeypatch
):
    _plan, _path, state = make_plan(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(deploy_upgrade.recovery, "backup_byte_limit", lambda _value: 10)
        with pytest.raises(ValueError, match="exceeds the backup bound"):
            deploy_upgrade.take_backup(state, tmp_path / "backups" / "big")
    assert not (tmp_path / "backups" / "big").exists()
    (tmp_path / "backups" / "taken").mkdir()
    with pytest.raises(ValueError, match="new directory"):
        deploy_upgrade.take_backup(state, tmp_path / "backups" / "taken")


# -- the command, with real processes for the services ------------------------------------------


def test_execute_through_the_command_with_real_service_processes(tmp_path, capsys, monkeypatch):
    import sys

    plan, path, state = make_plan(tmp_path, with_config=False)
    script = (
        "import json, sys, time; "
        "open(sys.argv[1], 'w').write(json.dumps({'schema': 'tessera.service-health.v1', "
        "'state': 'running', 'last_loop_at': time.time(), 'loops': 1}))"
    )
    document = json.loads(path.read_text())
    for service in document["services"]:
        service["stop_argv"] = [sys.executable, "-c", "pass"]
        service["restart_argv"] = [sys.executable, "-c", script, service["health"]["path"]]
    path.write_text(json.dumps(document))
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "deploy-upgrade", "--plan", str(path), "--execute", "--format", "json"],
    )
    assert main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["mode"] == "execute" and receipt["result"] == "ok"
    written = list((tmp_path / "receipts").glob("deploy-20*.json"))
    assert len(written) == 1  # the deploy receipt (the migrate receipt is deploy-fix-*)
    assert any(
        json.loads(p.read_text()).get("schema") == "tessera.deploy-receipt.v1" for p in written
    )
    with sqlite3.connect(state / DATABASE_NAME) as db:
        assert (
            db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]
            == LATEST_SCHEMA_VERSION
        )
    assert plan is not None


def test_a_bad_receipt_or_backup_directory_blocks_before_any_service_is_stopped(tmp_path):
    plan, path, _state = make_plan(tmp_path)
    for field in ("receipt_dir", "backup_dir"):
        document = json.loads(path.read_text())
        document[field] = str(tmp_path / "does-not-exist")
        path.write_text(json.dumps(document))
        fakes = Fakes()
        document = run(load_plan(path), fakes, execute=True)
        assert document["failed_step"] == "preflight"
        assert fakes.calls == [] and document["backup"] is None
        assert field.split("_")[0] in " ".join(document["preflight"]["blocking"])
