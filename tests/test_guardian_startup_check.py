"""Guardian configuration is closed, ``--check-config`` agrees with a real start, exit codes
separate a refused configuration (2) from a runtime failure (1), and the health file is written."""

import json
import os

import pytest

pytest.importorskip("cryptography")
from test_salad_safety_service import provision_tls
from test_salad_workload_jwt import ORG

from tessera_control import salad_safety_service as guardian
from tessera_control.exit_codes import CONFIG_CHECK_SCHEMA, CONFIG_REFUSED, OK, RUNTIME_FAILURE
from tessera_control.salad_workload_jwt import SaladWorkloadJWT
from tessera_worker.service_loop import HEALTH_SCHEMA


@pytest.fixture
def home(tmp_path):
    tmp_path.chmod(0o700)
    for name in ("state", "credentials", "run"):
        (tmp_path / name).mkdir(mode=0o700)
        (tmp_path / name).chmod(0o700)
    provision_tls(tmp_path)
    return tmp_path


def good_config(home):
    return {
        "state_directory": str(home / "state"),
        "credential_directory": str(home / "credentials"),
        "credential_reference": "credential-ref:salad-guardian",
        "credential_version": 1,
        "organization_ids": {"acme": ORG},
        "destination": "custody.example.invalid",
        "listen_host": "127.0.0.1",
        "listen_port": 9443,
        "tls_certificate": str(home / "cert.pem"),
        "tls_key": str(home / "key.pem"),
        "admin_token_file": str(home / "admin-token"),
        "total_cap_cents": 10000,
    }


def write_config(home, cfg, name="service.json"):
    path = home / name
    path.write_text(json.dumps(cfg))
    path.chmod(0o600)
    return str(path)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(SaladWorkloadJWT, "refresh", lambda self: None)


def bad_configs(home):
    base = good_config(home)
    corpus = {}
    corpus["unknown"] = {**base, "surprise": 1}
    corpus["misspelt_cap"] = {
        **{k: v for k, v in base.items() if k != "total_cap_cents"},
        "total_cap_cent": 100,
    }
    corpus["bool_frames"] = {**base, "max_open_billable_frames": True}
    corpus["string_frames"] = {**base, "max_open_billable_frames": "2"}
    corpus["frames_range"] = {**base, "max_open_billable_frames": 5}
    corpus["bool_cap"] = {**base, "total_cap_cents": True}
    corpus["bool_version"] = {**base, "credential_version": True}
    corpus["bool_port"] = {**base, "listen_port": True}
    corpus["missing_key"] = {k: v for k, v in base.items() if k != "tls_key"}
    corpus["wrong_type_path"] = {**base, "state_directory": 7}
    corpus["not_object"] = ["list"]
    corpus["socket_without_uid"] = {**base, "unix_socket": str(home / "run" / "g.sock")}
    return corpus


@pytest.mark.parametrize("label", sorted(bad_configs(__import__("pathlib").Path("/x"))))
def test_bad_config_refused_by_check_and_by_real_start(home, label, capsys):
    path = write_config(home, bad_configs(home)[label])
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED
    check_err = capsys.readouterr().err
    assert guardian.main(["--run", "--config", path]) == CONFIG_REFUSED
    captured = capsys.readouterr()
    assert captured.err == check_err
    assert '"reason":"config_refused"' in captured.out
    # --run alongside --check-config is ignored.
    assert guardian.main(["--run", "--check-config", "--config", path]) == CONFIG_REFUSED


def test_refusal_names_the_key_not_the_value(home, capsys):
    cfg = {**good_config(home), "max_open_billable_frames": True}
    path = write_config(home, cfg)
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED
    assert "max_open_billable_frames" in capsys.readouterr().err
    cfg = {k: v for k, v in good_config(home).items() if k != "total_cap_cents"}
    cfg["total_cap_cent"] = 100
    path = write_config(home, cfg)
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED
    err = capsys.readouterr().err
    assert "total_cap_cent" in err
    assert "100" not in err


def test_validator_matches_service_ranges(home):
    base = good_config(home)
    for key, value in (
        ("total_cap_cents", 0),
        ("allocation_deadline_seconds", 59),
        ("provisioning_deadline_seconds", 7201),
        ("max_prestart_rebinds", 11),
        ("prestart_read_deadline_seconds", 601),
    ):
        with pytest.raises(ValueError):
            guardian.validate_guardian_config({**base, key: value})
    params = guardian.validate_guardian_config({**base, "max_open_billable_frames": 4})
    assert params["max_open_billable_frames"] == 4


def test_missing_config_argument_is_a_refusal(capsys):
    assert guardian.main(["--run"]) == CONFIG_REFUSED
    assert guardian.main(["--check-config"]) == CONFIG_REFUSED
    assert guardian.main([]) == OK


def test_unsafe_config_file_mode_is_a_refusal(home):
    path = write_config(home, good_config(home))
    os.chmod(path, 0o644)
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED
    assert guardian.main(["--check-config", "--config", str(home / "absent.json")]) == (
        CONFIG_REFUSED
    )


def test_unsafe_named_files_are_refusals(home):
    path = write_config(home, good_config(home))
    os.chmod(home / "key.pem", 0o644)
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED
    os.chmod(home / "key.pem", 0o600)
    os.chmod(home / "state", 0o755)
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED


def test_valid_config_check_prints_one_json_line(home, capsys, monkeypatch):
    cfg = {**good_config(home), "max_open_billable_frames": 3}
    path = write_config(home, cfg)

    def forbidden(*args, **kwargs):
        raise AssertionError("check mode must not do this")

    monkeypatch.setattr(guardian, "SaladSafetyService", forbidden)
    monkeypatch.setattr(guardian, "SafetyHTTPServer", forbidden)
    monkeypatch.setattr(SaladWorkloadJWT, "refresh", forbidden)
    health = home / "health.json"
    assert guardian.main(["--check-config", "--config", path, "--health-file", str(health)]) == OK
    out = capsys.readouterr().out
    assert len(out.splitlines()) == 1
    document = json.loads(out)
    assert document["schema"] == CONFIG_CHECK_SCHEMA
    assert document["program"] == "salad-guardian"
    assert document["status"] == "ok"
    assert document["widths"] == {"max_open_billable_frames": 3}
    assert isinstance(document["notes"], list)
    assert not health.exists()
    assert sorted(p.name for p in (home / "state").iterdir()) == []


def test_check_with_socket_directory(home, capsys):
    cfg = {**good_config(home), "unix_socket": str(home / "run" / "g.sock"), "proxy_uid": 1000}
    path = write_config(home, cfg)
    assert guardian.main(["--check-config", "--config", path]) == OK
    assert not (home / "run" / "g.sock").exists()
    os.chmod(home / "run", 0o755)
    assert guardian.main(["--check-config", "--config", path]) == CONFIG_REFUSED


def test_jwks_failure_at_boot_is_a_runtime_failure(home, monkeypatch, capsys):
    path = write_config(home, good_config(home))

    def failing(self):
        raise TimeoutError("service_deadline")

    monkeypatch.setattr(SaladWorkloadJWT, "refresh", failing)
    assert guardian.main(["--run", "--config", path]) == RUNTIME_FAILURE
    assert "service_prerequisite_or_runtime_failure" in capsys.readouterr().out
    # The same file passes the check: nothing about it is refused.
    assert guardian.main(["--check-config", "--config", path]) == OK


def test_exception_escaping_serve_is_a_runtime_failure(home, monkeypatch, capsys):
    path = write_config(home, good_config(home))
    monkeypatch.setattr(guardian, "SafetyHTTPServer", lambda *a, **k: object())

    def raising(*args, **kwargs):
        raise RuntimeError("loop broke")

    monkeypatch.setattr(guardian, "serve", raising)
    health = home / "health.json"
    code = guardian.main(["--run", "--config", path, "--health-file", str(health)])
    assert code == RUNTIME_FAILURE
    assert "service_prerequisite_or_runtime_failure" in capsys.readouterr().out
    document = json.loads(health.read_text())
    assert document["state"] == "stopped"
    assert document["last_error_class"] == "RuntimeError"


def test_bind_failure_is_a_runtime_failure(home, monkeypatch):
    path = write_config(home, good_config(home))

    def failing(*args, **kwargs):
        raise OSError("address in use")

    monkeypatch.setattr(guardian, "SafetyHTTPServer", failing)
    assert guardian.main(["--run", "--config", path]) == RUNTIME_FAILURE


def test_health_file_schema_and_stop(home, monkeypatch):
    path = write_config(home, good_config(home))
    monkeypatch.setattr(guardian, "SafetyHTTPServer", lambda *a, **k: object())
    health = home / "health.json"
    seen = {}

    def fake_serve(service, server, *, health=None, **kwargs):
        seen["initial"] = json.loads(path_health.read_text())
        for _ in range(3):
            health.tick()
        seen["loops"] = health.loops

    path_health = health
    monkeypatch.setattr(guardian, "serve", fake_serve)
    assert guardian.main(["--run", "--config", path, "--health-file", str(health)]) == OK
    initial = seen["initial"]
    assert initial["schema"] == HEALTH_SCHEMA
    assert initial["name"] == "salad-guardian"
    assert initial["state"] == "running"
    assert initial["pid"] == os.getpid()
    assert set(initial) == {
        "schema",
        "name",
        "pid",
        "state",
        "started_at",
        "last_loop_at",
        "loops",
        "consecutive_errors",
        "last_error_class",
    }
    final = json.loads(health.read_text())
    assert final["state"] == "stopped"
    assert final["loops"] == 3
    assert final["consecutive_errors"] == 0
    assert oct(health.stat().st_mode & 0o777) == "0o600"


def test_health_writes_are_throttled_and_failures_do_not_stop(tmp_path, capsys):
    now = [0.0]
    writes = []
    health = guardian.GuardianHealth(
        tmp_path / "missing-dir" / "h.json", wall=lambda: 1000.0 + now[0], tick=lambda: now[0]
    )
    health.write = lambda state: (
        writes.append((now[0], state)) or setattr(health, "_last_write", now[0])
    )
    for second in range(0, 13):
        now[0] = float(second)
        health.tick()
    assert [at for at, _ in writes] == [0.0, 5.0, 10.0]
    # A real write into a missing directory fails quietly.
    real = guardian.GuardianHealth(tmp_path / "missing-dir" / "h.json")
    real.write("running")
    real.stop()
    assert "health_write_failed" in capsys.readouterr().err


def test_serve_without_health_is_unchanged():
    class Service:
        calls = 0

        def loop(self):
            self.calls += 1

    class Server:
        def handle_request(self):
            pass

    service = Service()
    guardian.serve(service, Server(), shutdown=lambda: service.calls >= 2)
    assert service.calls == 2
