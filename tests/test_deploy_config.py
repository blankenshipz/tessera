import ast
import inspect
import json
import os
import stat

import pytest

from tessera_control import deploy_config, salad_live_check
from tessera_control.campaign_salad_executor import CONFIG_SCHEMA, load_config
from tessera_control.deploy_config import (
    LIVE_CONFIG_KEYS,
    LIVE_CONFIG_SCHEMA,
    REMOVED_KEYS,
    apply_drop_removed_keys,
    check_config,
)

CANCEL = "cancel_credential_file"


def executor_document(base):
    for name in ("state", "cred", "work"):
        (base / name).mkdir(mode=0o700, exist_ok=True)
    for name in ("w", "t", "ca", "profiles"):
        (base / name).write_text("x")
        (base / name).chmod(0o600)
    return {
        "schema": CONFIG_SCHEMA,
        "state_dir": str(base / "state"),
        "profiles_file": str(base / "profiles"),
        "profile": "gpu-main",
        "worker_credential_file": str(base / "w"),
        "credential_directory": str(base / "cred"),
        "guardian_socket": "/run/guardian.sock",
        "guardian_uid": 1000,
        "custody": {
            "host": "custody.example",
            "port": 443,
            "token_file": str(base / "t"),
            "ca_file": str(base / "ca"),
            "certificate_sha256": "b" * 64,
        },
        "work_directory": str(base / "work"),
        "source_revision": salad_live_check.installed_source_pin("a" * 40),
        "poll_seconds": 5,
    }


def write(base, document, name="config.json", mode=0o600):
    base.chmod(0o700)
    path = base / name
    path.write_text(json.dumps(document))
    path.chmod(mode)
    return path


@pytest.fixture
def receipts(tmp_path):
    directory = tmp_path / "receipts"
    directory.mkdir(mode=0o700)
    return directory


def test_clean_config_is_ok(tmp_path):
    findings = check_config(write(tmp_path, executor_document(tmp_path)), label="1")
    assert [(f.check_id, f.status) for f in findings] == [
        ("config.1.keys", "ok"),
        ("config.1.source_pin", "ok"),
    ]


def test_removed_key_is_detected_with_the_automatic_fix(tmp_path):
    document = dict(executor_document(tmp_path), **{CANCEL: str(tmp_path / "SECRET-VALUE")})
    (finding,) = check_config(write(tmp_path, document))
    assert finding.status == "fail" and finding.check_id == "config.config.json.keys"
    assert finding.fix.fix_id == "drop_removed_keys" and finding.fix.automatic
    assert finding.detail["removed_keys"] == [CANCEL]
    assert "ADR 0075" in finding.detail["notes"][CANCEL]
    assert "SECRET-VALUE" not in json.dumps(finding.payload())


def test_unknown_key_is_manual_and_never_removed(tmp_path, receipts):
    document = dict(executor_document(tmp_path), surprise="v")
    path = write(tmp_path, document)
    (finding,) = check_config(path)
    assert finding.status == "fail" and finding.detail == {"unknown_keys": ["surprise"]}
    assert not finding.fix.automatic and finding.fix.command == "edit the file to drop the key"
    assert apply_drop_removed_keys(path, receipts) is None
    assert json.loads(path.read_text()) == document
    assert list(receipts.iterdir()) == []


def test_unknown_or_missing_schema_fails(tmp_path):
    for document in ({"schema": "nope"}, {"x": 1}):
        (finding,) = check_config(write(tmp_path, document))
        assert finding.status == "fail" and not finding.fix


def test_bad_files_fail_without_raising(tmp_path):
    good = write(tmp_path, executor_document(tmp_path))
    link = tmp_path / "link.json"
    link.symlink_to(good)
    dup = tmp_path / "dup.json"
    dup.write_text('{"schema": "a", "schema": "b"}')
    array = write(tmp_path, [1], name="array.json")
    big = tmp_path / "big.json"
    big.write_text(" " * 70000)
    for path in (link, dup, array, big, tmp_path / "missing.json", tmp_path, "relative.json"):
        (finding,) = check_config(path if not isinstance(path, str) else deploy_config.Path(path))
        assert finding.status == "fail"


def test_validation_errors_report_locations_only(tmp_path):
    document = executor_document(tmp_path)
    document["poll_seconds"] = 9999
    document["guardian_uid"] = "SECRET-VALUE"
    findings = check_config(write(tmp_path, document))
    assert [f.status for f in findings] == ["ok", "ok", "fail"]
    detail = findings[2].detail["errors"]
    assert {e["loc"] for e in detail} == {"poll_seconds", "guardian_uid"}
    assert all(set(e) == {"loc", "type"} for e in detail)
    assert "SECRET-VALUE" not in json.dumps(findings[2].payload())


def test_apply_removes_only_the_removed_key(tmp_path, receipts):
    document = dict(executor_document(tmp_path), **{CANCEL: str(tmp_path / "SECRET-VALUE")})
    path = write(tmp_path, document)
    before = path.read_bytes()
    receipt = apply_drop_removed_keys(path, receipts)
    expected = {k: v for k, v in document.items() if k != CANCEL}
    assert json.loads(path.read_text()) == expected
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert receipt["fix"] == "drop_removed_keys"
    assert receipt["config_schema"] == CONFIG_SCHEMA
    assert receipt["removed_keys"] == [CANCEL]
    assert receipt["before_sha256"] == deploy_config.sha256_hex(before)
    assert receipt["after_sha256"] == deploy_config.sha256_hex(path.read_bytes())
    (written,) = receipts.iterdir()
    text = written.read_text()
    assert "SECRET-VALUE" not in text and str(tmp_path) not in text
    assert apply_drop_removed_keys(path, receipts) is None
    assert len(list(receipts.iterdir())) == 1


def test_apply_refuses_group_readable_and_symlink(tmp_path, receipts):
    document = dict(executor_document(tmp_path), **{CANCEL: "x"})
    path = write(tmp_path, document, mode=0o640)
    with pytest.raises(ValueError):
        apply_drop_removed_keys(path, receipts)
    link = tmp_path / "link.json"
    link.symlink_to(write(tmp_path, document, name="real.json"))
    with pytest.raises(ValueError):
        apply_drop_removed_keys(link, receipts)
    assert json.loads(path.read_text()) == document
    assert list(receipts.iterdir()) == []


def test_fixed_executor_config_loads_with_the_real_loader(tmp_path, receipts):
    document = dict(executor_document(tmp_path), **{CANCEL: str(tmp_path / "w")})
    path = write(tmp_path, document)
    with pytest.raises(ValueError):
        load_config(path)
    apply_drop_removed_keys(path, receipts)
    assert load_config(path).profile == "gpu-main"
    assert [f.status for f in check_config(path)] == ["ok", "ok"]


def test_live_config_removed_key_is_handled(tmp_path, receipts):
    document = {key: "x" for key in LIVE_CONFIG_KEYS} | {"schema": LIVE_CONFIG_SCHEMA, CANCEL: "y"}
    path = write(tmp_path, document)
    (finding,) = check_config(path)
    assert finding.fix.fix_id == "drop_removed_keys"
    receipt = apply_drop_removed_keys(path, receipts)
    assert receipt["config_schema"] == LIVE_CONFIG_SCHEMA
    assert set(json.loads(path.read_text())) == LIVE_CONFIG_KEYS
    assert [f.status for f in check_config(path)] == ["ok"]


def test_live_config_key_set_matches_the_real_parser():
    tree = ast.parse(inspect.getsource(salad_live_check._config).lstrip())
    sets = [
        {element.value for element in node.elts}
        for node in ast.walk(tree)
        if isinstance(node, ast.Set) and all(isinstance(e, ast.Constant) for e in node.elts)
    ]
    assert set(LIVE_CONFIG_KEYS) in sets
    assert CANCEL not in LIVE_CONFIG_KEYS


def test_removed_table_names_both_schemas():
    assert set(REMOVED_KEYS) == {CONFIG_SCHEMA, LIVE_CONFIG_SCHEMA}
    assert all("ADR 0075" in REMOVED_KEYS[s][CANCEL] for s in REMOVED_KEYS)
