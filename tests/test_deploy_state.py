"""Deploy-check state, backup-cap and provider-file checks (ADR 0098)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tessera_control import deploy_state as ds
from tessera_control import provider_registration as pr
from tessera_control.migrations import LATEST_SCHEMA_VERSION
from tessera_control.salad_campaign_profiles import PROFILES_SCHEMA
from tessera_control.store import DATABASE_NAME, initialize_state

GIB = 1 << 30
GPU = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def state(tmp_path):
    path = tmp_path / "state"
    initialize_state(path)
    return path


def set_version(state: Path, version: int) -> None:
    with sqlite3.connect(state / DATABASE_NAME) as db:
        db.execute("UPDATE schema_meta SET schema_version=?", (version,))


def stored_version(state: Path) -> int:
    with sqlite3.connect(state / DATABASE_NAME) as db:
        return db.execute("SELECT schema_version FROM schema_meta").fetchone()[0]


# -- schema --------------------------------------------------------------------------------


def test_schema_ok_when_current(state):
    finding = ds.check_schema(state)
    assert finding.check_id == "state.schema" and finding.area == "migrations"
    assert finding.status == "ok" and finding.fix is None


def test_schema_pending_offers_an_automatic_migrate_fix(state):
    set_version(state, LATEST_SCHEMA_VERSION - 1)
    before = (state / DATABASE_NAME).read_bytes()
    finding = ds.check_schema(state)
    assert finding.status == "fail"
    assert finding.detail == {
        "current": LATEST_SCHEMA_VERSION - 1,
        "latest": LATEST_SCHEMA_VERSION,
        "pending": 1,
    }
    assert finding.fix.fix_id == "migrate" and finding.fix.automatic
    assert finding.fix.command == f"tessera migrate --state-dir {state}"
    assert (state / DATABASE_NAME).read_bytes() == before


def test_schema_newer_than_code_fails_without_a_fix(state):
    set_version(state, LATEST_SCHEMA_VERSION + 1)
    finding = ds.check_schema(state)
    assert finding.status == "fail" and finding.fix is None


def test_schema_missing_or_not_a_database_fails_and_creates_nothing(tmp_path):
    missing = tmp_path / "nothing"
    missing.mkdir()
    assert ds.check_schema(missing).status == "fail"
    assert not (missing / DATABASE_NAME).exists()
    (missing / DATABASE_NAME).write_text("not a database")
    assert ds.check_schema(missing).status == "fail"
    (missing / DATABASE_NAME).unlink()
    (missing / DATABASE_NAME).mkdir()
    assert ds.check_schema(missing).status == "fail"


# -- apply_migrate -------------------------------------------------------------------------


def test_apply_migrate_refuses_without_a_confirmed_backup(state, tmp_path):
    set_version(state, LATEST_SCHEMA_VERSION - 1)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    with pytest.raises(ValueError, match="backup"):
        ds.apply_migrate(state, receipts)
    assert stored_version(state) == LATEST_SCHEMA_VERSION - 1
    assert list(receipts.iterdir()) == []


def test_apply_migrate_applies_and_writes_a_receipt(state, tmp_path):
    set_version(state, LATEST_SCHEMA_VERSION - 1)
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    receipt = ds.apply_migrate(state, receipts, backup_confirmed=True)
    assert stored_version(state) == LATEST_SCHEMA_VERSION
    assert receipt is not None
    text = json.dumps(receipt)
    assert str(state) not in text
    written = [json.loads(p.read_text()) for p in receipts.iterdir()]
    assert len(written) == 1
    body = json.dumps(written[0])
    assert f'"schema_before": {LATEST_SCHEMA_VERSION - 1}' in body
    assert f'"schema_after": {LATEST_SCHEMA_VERSION}' in body
    assert ds.check_schema(state).status == "ok"


def test_apply_migrate_is_a_no_op_when_current(state, tmp_path):
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    assert ds.apply_migrate(state, receipts, backup_confirmed=True) is None
    assert list(receipts.iterdir()) == []


# -- backup cap ----------------------------------------------------------------------------


def grow_database(state: Path, size: int) -> None:
    with (state / DATABASE_NAME).open("r+b") as handle:
        handle.truncate(size)


def test_backup_cap_ok_for_a_small_state(state):
    finding = ds.check_backup_cap(state, None)
    assert finding.check_id == "backup.cap" and finding.area == "backup"
    assert finding.status == "ok" and finding.detail["cap_bytes"] == GIB


def test_backup_cap_counts_objects_and_ignores_symlinks(state, tmp_path):
    objects = state / "project-artifacts" / "tenant" / "project"
    objects.mkdir(parents=True)
    (objects / "a").write_bytes(b"x" * 1000)
    outside = tmp_path / "outside"
    outside.write_bytes(b"y" * 5000)
    (objects / "link").symlink_to(outside)
    size = (state / DATABASE_NAME).stat().st_size
    assert ds.check_backup_cap(state, None).detail["estimated_bytes"] == size + 1000


def test_backup_cap_warns_between_eighty_and_one_hundred_percent(state):
    grow_database(state, int(GIB * 0.9))
    finding = ds.check_backup_cap(state, None)
    assert finding.status == "warn" and finding.fix is None


def test_backup_cap_fails_over_the_cap_with_a_never_automatic_suggestion(state):
    grow_database(state, GIB + 1)
    finding = ds.check_backup_cap(state, None)
    assert finding.status == "fail"
    assert finding.detail["suggested_max_backup_bytes"] == 2 * GIB
    assert finding.fix.fix_id == "set_backup_cap" and finding.fix.automatic is False
    assert "--max-backup-bytes 2147483648" in finding.fix.command


def test_backup_cap_suggestion_keeps_a_quarter_of_headroom(state):
    grow_database(state, 3 * GIB)
    assert ds.check_backup_cap(state, None).detail["suggested_max_backup_bytes"] == 4 * GIB
    assert ds.check_backup_cap(state, 3 * GIB).status == "warn"


def test_backup_cap_honors_the_flag_and_rejects_bad_values(state):
    assert ds.check_backup_cap(state, 4 * GIB).detail["cap_bytes"] == 4 * GIB
    for bad in (1, 65 * GIB, True):
        finding = ds.check_backup_cap(state, bad)
        assert finding.status == "fail" and finding.fix is None


def test_backup_cap_has_no_suggestion_when_even_the_maximum_is_too_small(state):
    assert ds._suggest_cap(60 * GIB) is None
    assert ds._suggest_cap(52 * GIB) is None
    assert ds._suggest_cap(51 * GIB) == 64 * GIB


def test_backup_cap_fails_when_the_database_is_missing(tmp_path):
    assert ds.check_backup_cap(tmp_path, None).status == "fail"


# -- providers file ------------------------------------------------------------------------


def profile(name="cpu-main"):
    return dict(
        name=name,
        org_id="org-1",
        project_id="project-1",
        worker_principal_id="worker-salad-1",
        salad_organization="example-org",
        salad_project="tessera-research",
        country="US",
        cpu_millicores=2000,
        memory_mb=2048,
        disk_mb=1024,
        route_max_seconds=900,
        capability_environment_id="salad-campaign",
        artifact_destination="artifact-ref:quarantine",
        checkpoint_destination="checkpoint-ref:external",
        monitor_profile="a" * 64,
        watchdog_profile="b" * 64,
        custody_host="custody.example-tailnet.ts.net",
        credential_reference="credential-ref:salad-management",
        credential_version=1,
        allowed_image_digests=["sha256:" + "1" * 64],
        max_run_cents=50,
        max_task_cents=100,
        cpu_rate={"vcpu_hour_usd": "0.04", "memory_gb_hour_usd": "0.01"},
    )


def by_id(findings):
    return {f.check_id: f for f in findings}


def test_providers_valid_file_and_audit_chain(tmp_path):
    target = tmp_path / "providers.json"
    pr.apply_change(target, "add", "salad-profile", None, profile(), actor="test")
    before = (target.read_bytes(), Path(str(target) + pr.AUDIT_SUFFIX).read_bytes())
    found = by_id(ds.check_providers(target))
    assert found["providers.file"].status == "ok"
    assert found["providers.file"].detail["sections"] == {"salad-profile": 1}
    assert found["providers.audit"].status == "ok"
    assert found["providers.audit"].detail["records"] == 1
    assert before == (target.read_bytes(), Path(str(target) + pr.AUDIT_SUFFIX).read_bytes())


def test_providers_tampered_audit_fails(tmp_path):
    target = tmp_path / "providers.json"
    pr.apply_change(target, "add", "salad-profile", None, profile(), actor="test")
    audit = Path(str(target) + pr.AUDIT_SUFFIX)
    audit.write_text(audit.read_text().replace("test", "evil"))
    found = by_id(ds.check_providers(target))
    assert found["providers.file"].status == "ok"
    assert found["providers.audit"].status == "fail"


def test_providers_without_an_audit_file_skips_the_audit_check(tmp_path):
    target = tmp_path / "providers.json"
    target.write_text(json.dumps(pr.empty_document()))
    target.chmod(0o600)
    found = by_id(ds.check_providers(target))
    assert found["providers.file"].status == "ok"
    assert found["providers.audit"].status == "skipped"


def test_providers_invalid_inputs_fail_without_raising(tmp_path):
    relative = ds.check_providers(Path("providers.json"))
    assert [f.status for f in relative] == ["fail"]
    missing = ds.check_providers(tmp_path / "absent.json")
    assert [f.status for f in missing] == ["fail"]
    loose = tmp_path / "loose.json"
    loose.write_text(json.dumps(pr.empty_document()))
    loose.chmod(0o644)
    assert [f.status for f in ds.check_providers(loose)] == ["fail"]
    garbage = tmp_path / "garbage.json"
    garbage.write_text("{secret-looking-text")
    garbage.chmod(0o600)
    result = ds.check_providers(garbage)
    assert result[0].status == "fail" and "secret-looking-text" not in result[0].summary


# -- executor profiles ---------------------------------------------------------------------


def salad_profile(name):
    value = profile(name)
    value.pop("cpu_rate")
    value["gpu_class"] = GPU
    return value


def write_private(path: Path, document) -> Path:
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    path.chmod(0o600)
    return path


def executor_config(tmp_path, profiles_file, **names):
    document = {"schema": "ignored", "profiles_file": str(profiles_file), **names}
    return write_private(tmp_path / "executor.json", document)


@pytest.fixture
def profiles_file(tmp_path):
    tmp_path.chmod(0o700)
    document = {
        "schema": PROFILES_SCHEMA,
        "profiles": [salad_profile("gpu-main"), salad_profile("gpu-extra")],
    }
    return write_private(tmp_path / "profiles.json", document)


def test_executor_profiles_present(tmp_path, profiles_file):
    config = executor_config(tmp_path, profiles_file, profiles=["gpu-main", "gpu-extra"])
    (finding,) = ds.check_executor_profiles(config)
    assert finding.check_id == "providers.executor_profiles" and finding.status == "ok"


def test_executor_profile_missing_fails_per_name(tmp_path, profiles_file):
    config = executor_config(tmp_path, profiles_file, profiles=["gpu-main", "nope", "gone"])
    findings = ds.check_executor_profiles(config)
    assert [f.status for f in findings] == ["fail", "fail"]
    assert [f.detail["profile"] for f in findings] == ["nope", "gone"]


def test_executor_profiles_file_invalid_fails_without_echoing_contents(tmp_path):
    tmp_path.chmod(0o700)
    bad = write_private(tmp_path / "bad.json", '{"schema": "SECRET-VALUE"}')
    config = executor_config(tmp_path, bad, profile="gpu-main")
    (finding,) = ds.check_executor_profiles(config)
    assert finding.status == "fail" and "SECRET-VALUE" not in finding.summary


def test_executor_config_unparseable_is_a_single_failure(tmp_path, profiles_file):
    broken = write_private(tmp_path / "broken.json", "{nope")
    assert [f.status for f in ds.check_executor_profiles(broken)] == ["fail"]
    absent = tmp_path / "absent.json"
    assert [f.status for f in ds.check_executor_profiles(absent)] == ["fail"]
    both = executor_config(tmp_path, profiles_file, profile="a", profiles=["b"])
    assert [f.status for f in ds.check_executor_profiles(both)] == ["fail"]
    link = tmp_path / "link.json"
    link.symlink_to(both)
    assert [f.status for f in ds.check_executor_profiles(link)] == ["fail"]
