"""``tessera deploy-check``: the report, the exit code and the receipted fixes (ADR 0098)."""

from __future__ import annotations

import json

import pytest

from tessera_control import deploy_check
from tessera_control.cli import main
from tessera_control.deploy_check import Finding, Fix


def _finding(status: str, fix: Fix | None = None) -> Finding:
    return Finding(f"x.{status}", "test", status, f"a {status} finding", {"n": 1}, fix)


def test_a_report_is_ready_only_when_nothing_failed():
    ready = deploy_check.build_report([_finding("ok"), _finding("warn"), _finding("skipped")])
    assert ready["ready"] is True and deploy_check.exit_code(ready) == 0
    assert ready["counts"] == {"ok": 1, "warn": 1, "fail": 0, "skipped": 1}
    blocked = deploy_check.build_report([_finding("ok"), _finding("fail")])
    assert blocked["ready"] is False and deploy_check.exit_code(blocked) == 1
    assert blocked["schema"] == deploy_check.REPORT_SCHEMA


def test_the_text_summary_names_each_fix_and_how_to_apply_it():
    fix = Fix("drop_removed_keys", "drop a removed key", automatic=True)
    manual = Fix("set_backup_cap", "raise the cap", command="tessera backup --max-backup-bytes N")
    text = deploy_check.render_text(
        deploy_check.build_report([_finding("fail", fix), _finding("warn", manual)])
    )
    assert "--apply drop_removed_keys" in text
    assert "tessera backup --max-backup-bytes N" in text
    assert "NOT ready" in text


def test_a_finding_refuses_an_unknown_status():
    with pytest.raises(ValueError):
        Finding("x", "test", "maybe", "?")


def test_a_receipt_is_private_digest_named_and_never_overwritten(tmp_path):
    first = deploy_check.write_receipt(tmp_path, "migrate", {"schema_before": 70})
    second = deploy_check.write_receipt(tmp_path, "migrate", {"schema_before": 71})
    files = sorted(tmp_path.glob("deploy-fix-migrate-*.json"))
    assert len(files) == 2 and first["receipt_sha256"] != second["receipt_sha256"]
    assert all(oct(path.stat().st_mode & 0o777) == "0o600" for path in files)
    assert json.loads(files[0].read_text())["schema"] == deploy_check.RECEIPT_SCHEMA
    with pytest.raises(ValueError):
        deploy_check.write_receipt(tmp_path / "missing", "migrate", {})
    with pytest.raises(ValueError):
        deploy_check.write_receipt(type(tmp_path)("relative"), "migrate", {})


def test_the_command_with_nothing_to_check_is_ready_and_writes_a_private_report(
    tmp_path, capsys, monkeypatch
):
    report_file = tmp_path / "report.json"
    monkeypatch.setattr("sys.argv", ["tessera", "deploy-check", "--format", "json"])
    monkeypatch.setattr("sys.argv", [*__import__("sys").argv, "--report-file", str(report_file)])
    assert main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["ready"] is True
    assert [c["id"] for c in printed["checks"]] == ["smoke.cpu"]
    assert printed["checks"][0]["status"] == "skipped"
    assert json.loads(report_file.read_text())["schema"] == deploy_check.REPORT_SCHEMA
    assert oct(report_file.stat().st_mode & 0o777) == "0o600"


def test_apply_needs_a_receipt_directory_and_a_relative_report_path_is_refused(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setattr("sys.argv", ["tessera", "deploy-check", "--apply", "drop_removed_keys"])
    assert main() == 2
    assert "--receipt-dir" in capsys.readouterr().err
    monkeypatch.setattr(
        "sys.argv", ["tessera", "deploy-check", "--report-file", "relative-report.json"]
    )
    assert main() == 2
    assert "absolute" in capsys.readouterr().err


# -- the whole command over real files ---------------------------------------------------------


def _deployment(tmp_path):
    """A state directory one migration behind and an executor config naming a removed key."""
    import sqlite3

    from test_deploy_config import CANCEL, executor_document, write

    from tessera_control.migrations import LATEST_SCHEMA_VERSION
    from tessera_control.store import DATABASE_NAME, initialize_state

    state = tmp_path / "state"
    initialize_state(state)
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("UPDATE schema_meta SET schema_version=?", (LATEST_SCHEMA_VERSION - 1,))
    base = tmp_path / "executor"
    base.mkdir(mode=0o700)
    document = executor_document(base)
    document[CANCEL] = "/not/opened/cancel-token"
    config = write(base, document)
    receipts = tmp_path / "receipts"
    receipts.mkdir(mode=0o700)
    return state, config, receipts


def _run(monkeypatch, capsys, *argv):
    monkeypatch.setattr("sys.argv", ["tessera", "deploy-check", "--format", "json", *argv])
    code = main()
    return code, json.loads(capsys.readouterr().out)


def test_the_report_names_both_fixes_and_changes_nothing_without_apply(
    tmp_path, capsys, monkeypatch
):
    state, config, _ = _deployment(tmp_path)
    before = config.read_bytes()
    code, report = _run(
        monkeypatch, capsys, "--state-dir", str(state), "--executor-config", str(config)
    )
    checks = {c["id"]: c for c in report["checks"]}
    assert code == 1 and report["ready"] is False
    assert checks["state.schema"]["fix"]["id"] == "migrate"
    assert checks["config.1.keys"]["fix"]["id"] == "drop_removed_keys"
    assert checks["backup.cap"]["status"] in {"ok", "warn"}
    assert config.read_bytes() == before and report["applied"] == []


def test_apply_fixes_the_config_and_the_schema_and_writes_receipts(tmp_path, capsys, monkeypatch):
    state, config, receipts = _deployment(tmp_path)
    code, report = _run(
        monkeypatch,
        capsys,
        "--state-dir", str(state),
        "--executor-config", str(config),
        "--apply", "drop_removed_keys",
        "--apply", "migrate",
        "--backup-confirmed",
        "--receipt-dir", str(receipts),
    )  # fmt: skip
    checks = {c["id"]: c for c in report["checks"]}
    assert checks["state.schema"]["status"] == "ok"
    assert checks["config.1.keys"]["status"] == "ok"
    assert sorted(entry["fix"] for entry in report["applied"]) == ["drop_removed_keys", "migrate"]
    assert len(list(receipts.glob("deploy-fix-*.json"))) == 2
    assert "cancel_credential_file" not in config.read_text()
    text = "".join(path.read_text() for path in receipts.glob("deploy-fix-*.json"))
    assert str(tmp_path) not in text and "cancel-token" not in text
    # The executor's profiles file here is a placeholder, so that one check still fails.
    assert code == 1 and checks["providers.executor_profiles"]["status"] == "fail"
    # Running it again applies nothing and writes no further receipt.
    _, again = _run(
        monkeypatch, capsys, "--state-dir", str(state), "--executor-config", str(config),
        "--apply", "drop_removed_keys", "--apply", "migrate", "--backup-confirmed",
        "--receipt-dir", str(receipts),
    )  # fmt: skip
    assert again["applied"] == [] and len(list(receipts.glob("deploy-fix-*.json"))) == 2


def test_migrating_needs_the_operator_to_confirm_a_backup(tmp_path, capsys, monkeypatch):
    state, _config, receipts = _deployment(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "deploy-check", "--state-dir", str(state), "--apply", "migrate",
         "--receipt-dir", str(receipts)],
    )  # fmt: skip
    assert main() == 2
    assert "backup" in capsys.readouterr().err
    assert list(receipts.glob("deploy-fix-*.json")) == []


# -- an applied fix always has its receipt (review finding) ----------------------------------------


def _schema_version(state):
    import sqlite3

    from tessera_control.store import DATABASE_NAME

    with sqlite3.connect(state / DATABASE_NAME) as db:
        return db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]


@pytest.mark.parametrize("which", ["missing", "relative", "file"])
def test_a_bad_receipt_directory_changes_nothing(tmp_path, capsys, monkeypatch, which):
    state, config, _ = _deployment(tmp_path)
    bad = {
        "missing": str(tmp_path / "no-such-dir"),
        "relative": "receipts",
        "file": str(config),
    }[which]
    before_config, before_schema = config.read_bytes(), _schema_version(state)
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "deploy-check", "--state-dir", str(state), "--executor-config", str(config),
         "--apply", "drop_removed_keys", "--apply", "migrate", "--backup-confirmed",
         "--receipt-dir", bad],
    )  # fmt: skip
    assert main() == 2
    assert "receipt directory" in capsys.readouterr().err
    assert config.read_bytes() == before_config and _schema_version(state) == before_schema


def test_a_read_only_receipt_directory_changes_nothing(tmp_path, capsys, monkeypatch):
    import os

    if os.geteuid() == 0:
        pytest.skip("root can write to a read-only directory")
    state, config, receipts = _deployment(tmp_path)
    receipts.chmod(0o500)
    before_config, before_schema = config.read_bytes(), _schema_version(state)
    monkeypatch.setattr(
        "sys.argv",
        ["tessera", "deploy-check", "--state-dir", str(state), "--executor-config", str(config),
         "--apply", "drop_removed_keys", "--apply", "migrate", "--backup-confirmed",
         "--receipt-dir", str(receipts)],
    )  # fmt: skip
    try:
        assert main() == 2
    finally:
        receipts.chmod(0o700)
    assert "not writable" in capsys.readouterr().err
    assert config.read_bytes() == before_config and _schema_version(state) == before_schema


def test_a_bad_report_path_is_refused_before_any_fix_is_applied(tmp_path, capsys, monkeypatch):
    state, config, receipts = _deployment(tmp_path)
    before_config, before_schema = config.read_bytes(), _schema_version(state)
    for report in ("report.json", str(tmp_path / "missing-dir" / "report.json"), str(tmp_path)):
        monkeypatch.setattr(
            "sys.argv",
            ["tessera", "deploy-check", "--state-dir", str(state), "--executor-config", str(config),
             "--apply", "drop_removed_keys", "--apply", "migrate", "--backup-confirmed",
             "--receipt-dir", str(receipts), "--report-file", report],
        )  # fmt: skip
        assert main() == 2
        capsys.readouterr()
        assert config.read_bytes() == before_config and _schema_version(state) == before_schema
        assert list(receipts.glob("deploy-fix-*.json")) == []


def test_the_fixes_themselves_refuse_a_bad_receipt_directory_before_changing_anything(tmp_path):
    from tessera_control import deploy_config, deploy_state

    state, config, _ = _deployment(tmp_path)
    before_config, before_schema = config.read_bytes(), _schema_version(state)
    for bad in (tmp_path / "gone", type(tmp_path)("relative")):
        with pytest.raises(ValueError, match="receipt directory"):
            deploy_config.apply_drop_removed_keys(config, bad)
        with pytest.raises(ValueError, match="receipt directory"):
            deploy_state.apply_migrate(state, bad, backup_confirmed=True)
    assert config.read_bytes() == before_config and _schema_version(state) == before_schema
