from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from tessera_control.deploy_upgrade_plan import (
    PLAN_SCHEMA,
    HealthFile,
    HealthHttp,
    PlanError,
    load_plan,
)
from tessera_control.recovery import MAX_BACKUP_BYTES, MAX_CONFIGURABLE_BACKUP_BYTES

SENTINEL = "zz-sentinel-value-7431"


def _service(name: str = "api", **extra) -> dict:
    return {
        "name": name,
        "restart_argv": ["systemctl", "restart", name],
        "health": {"kind": "http", "url": "http://127.0.0.1:8080/health"},
        **extra,
    }


def _plan(root: Path, **extra) -> dict:
    return {
        "schema": PLAN_SCHEMA,
        "state_dir": str(root / "state"),
        "backup_dir": str(root / "backup"),
        "receipt_dir": str(root / "receipts"),
        "services": [_service()],
        **extra,
    }


def _write(root: Path, document, name: str = "plan.json", mode: int = 0o600) -> Path:
    path = root / name
    path.write_text(document if isinstance(document, str) else json.dumps(document))
    path.chmod(mode)
    return path


def _refused(root: Path, document) -> str:
    with pytest.raises(PlanError) as caught:
        load_plan(_write(root, document))
    return str(caught.value)


def test_minimal_plan_loads_and_round_trips(tmp_path):
    plan = load_plan(_write(tmp_path, _plan(tmp_path)))
    assert plan.schema_id == PLAN_SCHEMA
    assert plan.service_names == ("api",)
    assert plan.smoke_cpu is False
    assert plan.max_backup_bytes is None
    service = plan.services[0]
    assert (service.health_wait_seconds, service.command_timeout_seconds) == (120, 60)
    assert isinstance(service.health, HealthHttp) and service.health.timeout_seconds == 5
    again = type(plan).model_validate(plan.model_dump(by_alias=True))
    assert again == plan


def test_both_health_kinds_parse(tmp_path):
    file_health = {"kind": "file", "path": str(tmp_path / "beat"), "max_age_seconds": 30}
    document = _plan(tmp_path, services=[_service("a", health=file_health), _service("b")])
    plan = load_plan(_write(tmp_path, document))
    assert isinstance(plan.services[0].health, HealthFile)
    assert isinstance(plan.services[1].health, HealthHttp)
    assert plan.service_names == ("a", "b")


def test_optional_fields_accepted(tmp_path):
    document = _plan(
        tmp_path,
        source_revision="a" * 40,
        executor_configs=[str(tmp_path / "e.json")],
        live_configs=[str(tmp_path / "l.json")],
        providers_file=str(tmp_path / "p.json"),
        smoke_cpu=True,
    )
    plan = load_plan(_write(tmp_path, document))
    assert plan.smoke_cpu and plan.source_revision == "a" * 40


def test_unknown_key_refused(tmp_path):
    message = _refused(tmp_path, _plan(tmp_path, **{SENTINEL: 1}))
    assert SENTINEL not in message


def test_unknown_nested_key_refused(tmp_path):
    document = _plan(tmp_path)
    document["services"][0]["extra"] = 1
    assert "services" in _refused(tmp_path, document)


def test_duplicate_json_key_refused(tmp_path):
    text = json.dumps(_plan(tmp_path))[:-1] + ', "smoke_cpu": true, "smoke_cpu": false}'
    _refused(tmp_path, text)


def test_wrong_schema_refused(tmp_path):
    document = _plan(tmp_path)
    document["schema"] = SENTINEL
    assert SENTINEL not in _refused(tmp_path, document)


def test_non_json_and_non_object_refused(tmp_path):
    _refused(tmp_path, "not json")
    _refused(tmp_path, "[1, 2]")


def test_group_readable_refused(tmp_path):
    path = _write(tmp_path, _plan(tmp_path), mode=0o640)
    with pytest.raises(PlanError):
        load_plan(path)


def test_symlink_refused(tmp_path):
    real = _write(tmp_path, _plan(tmp_path), name="real.json")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(PlanError):
        load_plan(link)


def test_relative_plan_path_and_missing_and_oversize_refused(tmp_path):
    path = _write(tmp_path, _plan(tmp_path))
    with pytest.raises(PlanError):
        load_plan(os.path.relpath(path))
    with pytest.raises(PlanError):
        load_plan(tmp_path / "missing.json")
    big = _write(tmp_path, " " * 70000, name="big.json")
    with pytest.raises(PlanError):
        load_plan(big)


@pytest.mark.parametrize("field", ["state_dir", "backup_dir", "receipt_dir", "providers_file"])
def test_relative_path_fields_refused(tmp_path, field):
    document = _plan(tmp_path, **{field: f"{SENTINEL}/rel"})
    message = _refused(tmp_path, document)
    assert field in message and SENTINEL not in message


@pytest.mark.parametrize("field", ["executor_configs", "live_configs"])
def test_relative_config_lists_refused(tmp_path, field):
    message = _refused(tmp_path, _plan(tmp_path, **{field: [f"{SENTINEL}.json"]}))
    assert field in message and SENTINEL not in message


def test_relative_health_file_path_refused(tmp_path):
    health = {"kind": "file", "path": f"{SENTINEL}/beat", "max_age_seconds": 30}
    message = _refused(tmp_path, _plan(tmp_path, services=[_service(health=health)]))
    assert SENTINEL not in message


def test_config_lists_bounded(tmp_path):
    paths = [str(tmp_path / f"c{i}.json") for i in range(5)]
    _refused(tmp_path, _plan(tmp_path, executor_configs=paths))


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/health",
        "http://localhost:9000/ready",
        "http://[::1]:8080/health",
    ],
)
def test_loopback_urls_accepted(tmp_path, url):
    health = {"kind": "http", "url": url}
    plan = load_plan(_write(tmp_path, _plan(tmp_path, services=[_service(health=health)])))
    assert plan.services[0].health.url == url


@pytest.mark.parametrize(
    "url",
    [
        f"http://{SENTINEL}.example:8080/health",
        f"https://127.0.0.1:8080/{SENTINEL}",
        f"http://user:{SENTINEL}@127.0.0.1:8080/health",
        f"http://127.0.0.1:8080/health?{SENTINEL}=1",
        f"http://127.0.0.1:8080/health#{SENTINEL}",
        f"http://127.0.0.1/{SENTINEL}",
        "http://127.0.0.1:8080",
        "http://10.0.0.1:8080/health",
        "http://127.0.0.1:99999/health",
        "ftp://127.0.0.1:8080/health",
    ],
)
def test_other_urls_refused(tmp_path, url):
    health = {"kind": "http", "url": url}
    message = _refused(tmp_path, _plan(tmp_path, services=[_service(health=health)]))
    assert SENTINEL not in message


def test_health_bounds(tmp_path):
    for health in (
        {"kind": "http", "url": "http://127.0.0.1:1/x", "timeout_seconds": 0},
        {"kind": "http", "url": "http://127.0.0.1:1/x", "timeout_seconds": 61},
        {"kind": "file", "path": str(tmp_path / "b"), "max_age_seconds": 4},
        {"kind": "file", "path": str(tmp_path / "b"), "max_age_seconds": 86401},
        {"kind": "other"},
    ):
        _refused(tmp_path, _plan(tmp_path, services=[_service(health=health)]))


@pytest.mark.parametrize(
    "argv",
    [
        [],
        [""],
        ["x"] * 17,
        ["a" * 513],
        ["ok", "bad\0nul"],
        ["bin/relative", "x"],
        ["./local"],
        [1],
    ],
)
def test_bad_argv_refused(tmp_path, argv):
    for key in ("restart_argv", "stop_argv"):
        document = _plan(tmp_path)
        document["services"][0][key] = argv
        _refused(tmp_path, document)


def test_argv_forms_accepted(tmp_path):
    service = _service(
        restart_argv=["/usr/bin/systemctl", "restart", "a b; $(x)"],
        stop_argv=["x"] * 16,
    )
    plan = load_plan(_write(tmp_path, _plan(tmp_path, services=[service])))
    assert plan.services[0].restart_argv[2] == "a b; $(x)"
    assert len(plan.services[0].stop_argv) == 16


def test_argv_value_never_in_error(tmp_path):
    document = _plan(tmp_path)
    document["services"][0]["restart_argv"] = [f"rel/{SENTINEL}"]
    assert SENTINEL not in _refused(tmp_path, document)


@pytest.mark.parametrize("name", ["", "Api", "1a", "a b", "a" * 33, "a/b"])
def test_bad_service_names_refused(tmp_path, name):
    _refused(tmp_path, _plan(tmp_path, services=[_service(name)]))


def test_duplicate_service_names_refused(tmp_path):
    _refused(tmp_path, _plan(tmp_path, services=[_service("a"), _service("a")]))


def test_service_count_bounds(tmp_path):
    ok = [_service(f"s{i}") for i in range(8)]
    assert len(load_plan(_write(tmp_path, _plan(tmp_path, services=ok))).services) == 8
    _refused(tmp_path, _plan(tmp_path, services=ok + [_service("s8")]))
    _refused(tmp_path, _plan(tmp_path, services=[]))


def test_service_timing_bounds(tmp_path):
    for extra in (
        {"health_wait_seconds": 4},
        {"health_wait_seconds": 901},
        {"command_timeout_seconds": 4},
        {"command_timeout_seconds": 301},
    ):
        _refused(tmp_path, _plan(tmp_path, services=[_service(**extra)]))


@pytest.mark.parametrize(
    "value", [MAX_BACKUP_BYTES - 1, MAX_CONFIGURABLE_BACKUP_BYTES + 1, 0, -1, "1073741824", True]
)
def test_backup_bytes_outside_bounds_refused(tmp_path, value):
    message = _refused(tmp_path, _plan(tmp_path, max_backup_bytes=value))
    assert "max_backup_bytes" in message


@pytest.mark.parametrize(
    "value", [MAX_BACKUP_BYTES, 2 * MAX_BACKUP_BYTES, MAX_CONFIGURABLE_BACKUP_BYTES]
)
def test_backup_bytes_inside_bounds_accepted(tmp_path, value):
    plan = load_plan(_write(tmp_path, _plan(tmp_path, max_backup_bytes=value)))
    assert plan.max_backup_bytes == value


def test_source_revision_pattern(tmp_path):
    message = _refused(tmp_path, _plan(tmp_path, source_revision=SENTINEL))
    assert SENTINEL not in message
    _refused(tmp_path, _plan(tmp_path, source_revision="A" * 40))


def test_errors_never_echo_values_anywhere(tmp_path):
    document = copy.deepcopy(_plan(tmp_path))
    document["smoke_cpu"] = SENTINEL
    document["services"][0]["health_wait_seconds"] = SENTINEL
    assert SENTINEL not in _refused(tmp_path, document)
