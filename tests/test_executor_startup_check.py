"""``--check-config`` and the exit codes of the Salad executor's startup (ADR 0214).

Exit 2 only when the operator's configuration is refused; every other setup failure exits 1 so a
supervisor restarts it. ``--check-config`` shares the configuration checks and takes no lock,
opens no database and reads no credential.
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest
from test_deploy_config import executor_document, write

from tessera_control import campaign_salad_executor as module
from tessera_control import exit_codes, maintenance_lock, salad_live_check
from tessera_control.store import Forbidden

STALE = "a" * 40 + "+sha256:" + "b" * 64
STALE_NOTE = "source_revision is stale for this release; deploy-upgrade re-pins it"


@pytest.fixture(autouse=True)
def _profiles(monkeypatch):
    known = {"gpu-main": SimpleNamespace(custody_host="custody.example")}

    class Profiles:
        def get(self, name):
            return known.get(name)

    monkeypatch.setattr(module, "load_profiles", lambda path: Profiles())
    yield
    maintenance_lock._release_process_locks()


@pytest.fixture
def forbidden_effects(monkeypatch):
    """Check mode must not take the lock, open the database or read the credential."""

    def boom(*args, **kwargs):
        raise AssertionError("check mode reached an effectful step")

    import tessera_control.store as store

    monkeypatch.setattr(maintenance_lock, "hold_shared_for_process", boom)
    monkeypatch.setattr(store, "Store", boom)
    monkeypatch.setattr(module, "_authenticate_executor_worker", boom)
    monkeypatch.setattr(module, "build_executor", boom)


def _file(tmp_path, **over):
    document = dict(executor_document(tmp_path), **over)
    return write(tmp_path, {k: v for k, v in document.items() if v is not ...})


def _custody(tmp_path, **over):
    document = executor_document(tmp_path)
    return _file(tmp_path, custody=dict(document["custody"], **over))


def _bad_files(tmp_path):
    return {
        "unknown key": lambda: _file(tmp_path, surprise=1),
        "bool width": lambda: _file(tmp_path, max_concurrent_paid_routes=True),
        "width too large": lambda: _file(tmp_path, max_concurrent_paid_routes=5),
        "width zero": lambda: _file(tmp_path, max_concurrent_paid_routes=0),
        "unknown profile": lambda: _file(tmp_path, profile="no-such"),
        "custody differs": lambda: _custody(tmp_path, workload_host="other.example"),
        "malformed pin": lambda: _file(tmp_path, source_revision="not-a-pin"),
        "malformed watcher": lambda: _file(tmp_path, stage_watcher={"enabled": "yes"}),
    }


CASES = [
    "unknown key",
    "bool width",
    "width too large",
    "width zero",
    "unknown profile",
    "custody differs",
    "malformed pin",
    "malformed watcher",
]


@pytest.mark.parametrize("case", CASES)
def test_a_refused_file_exits_2_in_check_and_in_a_real_start(tmp_path, capsys, case):
    path = _bad_files(tmp_path)[case]()
    assert module.main(["--config", str(path), "--check-config"]) == exit_codes.CONFIG_REFUSED
    check = capsys.readouterr()
    assert json.loads(check.out) == {"status": "refused", "reason": "executor_config_refused"}
    assert module.main(["--config", str(path), "--once"]) == exit_codes.CONFIG_REFUSED
    start = capsys.readouterr()
    assert json.loads(start.out) == {"status": "refused", "reason": "executor_config_refused"}
    assert check.err == start.err


def test_a_valid_config_checks_ok_with_its_width(tmp_path, capsys, forbidden_effects):
    path = _file(tmp_path, max_concurrent_paid_routes=3)
    assert module.main(["--config", str(path), "--check-config"]) == exit_codes.OK
    assert json.loads(capsys.readouterr().out) == {
        "schema": exit_codes.CONFIG_CHECK_SCHEMA,
        "program": "salad-executor",
        "status": "ok",
        "widths": {"max_concurrent_paid_routes": 3},
        "notes": [],
    }


def test_the_width_defaults_to_one(tmp_path, capsys, forbidden_effects):
    assert module.main(["--config", str(_file(tmp_path)), "--check-config"]) == 0
    assert json.loads(capsys.readouterr().out)["widths"] == {"max_concurrent_paid_routes": 1}


def test_a_stale_but_well_formed_pin_is_a_note_in_check_and_a_refusal_at_start(
    tmp_path, capsys, forbidden_effects
):
    assert salad_live_check.installed_source_pin("a" * 40) != STALE
    path = _file(tmp_path, source_revision=STALE)
    assert module.main(["--config", str(path), "--check-config"]) == exit_codes.OK
    assert json.loads(capsys.readouterr().out)["notes"] == [STALE_NOTE]
    assert module.main(["--config", str(path), "--once"]) == exit_codes.CONFIG_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == "executor_config_refused"


def test_a_stale_pin_does_not_hide_another_refusal_in_check(tmp_path, capsys):
    path = _file(tmp_path, source_revision=STALE, profile="no-such")
    assert module.main(["--config", str(path), "--check-config"]) == exit_codes.CONFIG_REFUSED


@pytest.mark.parametrize(
    "fault", [sqlite3.OperationalError("database is locked"), OSError("disk")], ids=type
)
def test_a_fault_taking_the_lock_exits_1(tmp_path, capsys, monkeypatch, fault):
    def fail(state_dir):
        raise fault

    monkeypatch.setattr(maintenance_lock, "hold_shared_for_process", fail)
    assert module.main(["--config", str(_file(tmp_path)), "--once"]) == exit_codes.RUNTIME_FAILURE
    assert json.loads(capsys.readouterr().out) == {
        "status": "unavailable",
        "reason": "executor_setup_failed",
        "error_class": type(fault).__name__,
    }


def test_a_busy_maintenance_lock_exits_1(tmp_path, monkeypatch):
    def busy(state_dir):
        raise maintenance_lock.MaintenanceBusy("busy")

    monkeypatch.setattr(maintenance_lock, "hold_shared_for_process", busy)
    assert module.main(["--config", str(_file(tmp_path)), "--once"]) == exit_codes.RUNTIME_FAILURE


def _store_raising(monkeypatch, error):
    import tessera_control.store as store

    class Store:
        def __init__(self, state_dir):
            pass

        def authenticate_worker(self, token):
            raise error

    monkeypatch.setattr(store, "Store", Store)


def test_the_database_failing_during_authentication_exits_1(tmp_path, capsys, monkeypatch):
    _store_raising(monkeypatch, sqlite3.OperationalError("unable to open database file"))
    assert module.main(["--config", str(_file(tmp_path)), "--once"]) == exit_codes.RUNTIME_FAILURE
    out = json.loads(capsys.readouterr().out)
    assert out["error_class"] == "OperationalError" and "unable" not in json.dumps(out)


def test_a_credential_the_database_does_not_accept_is_a_config_refusal(
    tmp_path, capsys, monkeypatch
):
    _store_raising(monkeypatch, Forbidden("Unauthorized"))
    assert module.main(["--config", str(_file(tmp_path)), "--once"]) == exit_codes.CONFIG_REFUSED
    assert json.loads(capsys.readouterr().out)["reason"] == "executor_config_refused"


def test_an_empty_credential_file_is_a_config_refusal(tmp_path, capsys, monkeypatch):
    path = _file(tmp_path)
    (tmp_path / "w").write_text("")
    _store_raising(monkeypatch, AssertionError("never reached"))
    assert module.main(["--config", str(path), "--once"]) == exit_codes.CONFIG_REFUSED


def test_check_config_touches_no_lock_database_or_executor(tmp_path, forbidden_effects):
    assert module.main(["--config", str(_file(tmp_path)), "--check-config"]) == exit_codes.OK
