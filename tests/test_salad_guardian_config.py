"""The guardian's closed config, through the one parser (``validate_guardian_config``) that a
real start and ``--check-config`` both use (ADR 0214, ADR 0215): every refusal names a key or a
check, never a value."""

import json
import os

import pytest

from tessera_control import salad_safety_service as service
from tessera_control.exit_codes import CONFIG_REFUSED

SECRET = "/very/secret/path-VALUE-7f3a"


def minimal():
    return {
        "state_directory": SECRET + "/state",
        "credential_directory": SECRET + "/cred",
        "credential_reference": "credential-ref:salad-guardian",
        "credential_version": 1,
        "organization_ids": {"org": "00000000-0000-4000-8000-000000000000"},
        "destination": "custody.example.invalid",
        "listen_host": "127.0.0.1",
        "listen_port": 9443,
        "tls_certificate": SECRET + "/cert.pem",
        "tls_key": SECRET + "/key.pem",
        "admin_token_file": SECRET + "/token",
    }


def write(tmp_path, cfg, mode=0o600):
    path = tmp_path / "guardian.json"
    path.write_text(json.dumps(cfg))
    os.chmod(path, mode)
    return path


def refusal(raw):
    with pytest.raises(ValueError) as caught:
        service.validate_guardian_config(raw)
    message = str(caught.value)
    assert "VALUE-7f3a" not in message
    return message


def test_minimal_validates_with_todays_defaults():
    assert service.validate_guardian_config(minimal()) == {
        "allocation_deadline_seconds": service.DEFAULT_ALLOCATION_SECONDS,
        "provisioning_deadline_seconds": None,
        "max_open_billable_frames": 1,
        "total_cap_cents": None,
        "max_prestart_rebinds": 3,
        "prestart_read_deadline_seconds": service.DEFAULT_PRESTART_READ_DEADLINE_SECONDS,
    }


def test_full_config_validates():
    raw = minimal() | {
        "total_cap_cents": 10000,
        "allocation_deadline_seconds": 120,
        "provisioning_deadline_seconds": 600,
        "control_origin": "http://127.0.0.1:8000",
        "max_open_billable_frames": 4,
        "max_prestart_rebinds": 0,
        "prestart_read_deadline_seconds": 30,
        "unix_socket": "/run/x/s.sock",
        "proxy_uid": 1000,
    }
    assert service.validate_guardian_config(raw)["max_open_billable_frames"] == 4


@pytest.mark.parametrize(
    "key", ["total_cap_cents", "provisioning_deadline_seconds", "control_origin", "unix_socket"]
)
def test_null_means_unset_for_nullable_keys(key):
    service.validate_guardian_config(minimal() | {key: None})


@pytest.mark.parametrize("typo", ["total_cap_cent", "max_open_billable_frame", "bogus"])
def test_unknown_key_refuses_and_is_named(typo):
    assert refusal(minimal() | {typo: 5}) == "unknown_config_key: " + typo


def test_unknown_key_echo_is_cut_to_64_characters():
    assert refusal(minimal() | {"k" * 200: 1}) == "unknown_config_key: " + "k" * 64


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("total_cap_cents", 0, "invalid_total_cap"),
        ("total_cap_cents", 1_000_001, "invalid_total_cap"),
        ("total_cap_cents", True, "invalid_config_value: total_cap_cents"),
        ("total_cap_cents", 1.0, "invalid_config_value: total_cap_cents"),
        ("max_open_billable_frames", 0, "invalid_max_open_billable_frames"),
        (
            "max_open_billable_frames",
            service.MAX_OPEN_BILLABLE_FRAMES_LIMIT + 1,
            "invalid_max_open_billable_frames",
        ),
        ("max_open_billable_frames", True, "invalid_config_value: max_open_billable_frames"),
        ("max_open_billable_frames", None, "invalid_config_value: max_open_billable_frames"),
        ("allocation_deadline_seconds", 59, "invalid_allocation_deadline"),
        ("allocation_deadline_seconds", False, "invalid_config_value: allocation_deadline_seconds"),
        ("provisioning_deadline_seconds", 119, "invalid_provisioning_deadline"),
        ("max_prestart_rebinds", -1, "invalid_max_prestart_rebinds"),
        ("max_prestart_rebinds", True, "invalid_config_value: max_prestart_rebinds"),
        ("prestart_read_deadline_seconds", -1, "invalid_prestart_read_deadline"),
        (
            "prestart_read_deadline_seconds",
            True,
            "invalid_config_value: prestart_read_deadline_seconds",
        ),
        ("prestart_read_deadline_seconds", 10_000, "invalid_prestart_read_deadline"),
        ("prestart_read_deadline_seconds", float("nan"), "invalid_prestart_read_deadline"),
        ("listen_port", 0, "invalid_config_value: listen_port"),
        ("listen_port", 65536, "invalid_config_value: listen_port"),
        ("listen_port", True, "invalid_config_value: listen_port"),
        ("credential_version", True, "invalid_config_value: credential_version"),
        ("credential_version", 0, "invalid_config_value: credential_version"),
        ("credential_version", 2**31, "invalid_config_value: credential_version"),
        ("proxy_uid", True, "invalid_config_value: proxy_uid"),
        ("proxy_uid", None, "invalid_config_value: proxy_uid"),
        ("destination", 5, "invalid_config_value: destination"),
        ("destination", "", "invalid_config_value: destination"),
        ("destination", "a\x00b", "invalid_config_value: destination"),
        ("state_directory", SECRET + "x" * 4096, "invalid_config_value: state_directory"),
        ("organization_ids", {}, "invalid_config_value: organization_ids"),
        ("organization_ids", {"org": ""}, "invalid_config_value: organization_ids"),
        ("control_origin", "not an origin VALUE-7f3a", "invalid_config_value: control_origin"),
    ],
)
def test_invalid_values_refuse_by_key_without_echo(key, value, expected):
    assert refusal(minimal() | {key: value}) == expected


def test_unix_socket_and_proxy_uid_go_together():
    expected = "invalid_config_value: unix_socket_and_proxy_uid_go_together"
    assert refusal(minimal() | {"unix_socket": "/run/x/s.sock"}) == expected
    assert refusal(minimal() | {"proxy_uid": 1000}) == expected


def test_missing_key_named_and_values_not_echoed():
    raw = minimal()
    del raw["tls_key"]
    assert refusal(raw) == "missing_config_key: tls_key"
    message = refusal(minimal() | {"state_directory": 12345678})
    assert message == "invalid_config_value: state_directory" and "12345678" not in message


def test_file_checks_stay(tmp_path):
    with pytest.raises(service.GuardianConfigRefused):
        service.prepare_guardian_startup(write(tmp_path, minimal(), mode=0o644))
    path = tmp_path / "dup.json"
    path.write_text('{"listen_port": 1, "listen_port": 2}')
    os.chmod(path, 0o600)
    with pytest.raises(service.GuardianConfigRefused):
        service.prepare_guardian_startup(path)


def test_main_and_check_config_refuse_unknown_key_alike_before_jwt(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise AssertionError("JWT refresh must not run")

    monkeypatch.setattr(service.SaladWorkloadJWT, "refresh", boom)
    path = str(write(tmp_path, minimal() | {"total_cap_cent": 5}))
    assert CONFIG_REFUSED == 2
    assert service.main(["--run", "--config", path]) == CONFIG_REFUSED
    captured = capsys.readouterr()
    assert json.loads(captured.out.strip()) == {"state": "unavailable", "reason": "config_refused"}
    assert captured.err.strip() == "unknown_config_key: total_cap_cent"
    assert service.main(["--check-config", "--config", path]) == CONFIG_REFUSED
    assert capsys.readouterr().err.strip() == "unknown_config_key: total_cap_cent"
