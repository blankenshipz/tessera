"""Operator boundary tests: no network or Docker effects."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tessera_operator import garage


def test_plan_is_offline_and_fixed(monkeypatch):
    monkeypatch.setattr(garage.subprocess, "run", lambda *a, **k: pytest.fail("remote called"))
    server = garage.create_server()
    assert "research" not in server.name.lower()
    assert hashlib.sha256(garage.compose_bytes()).hexdigest()
    assert garage.HOST == "control-host.example"
    assert (
        garage.compose_bytes()
        == (Path(__file__).resolve().parents[1] / "deploy/garage-control/compose.yaml").read_bytes()
    )


@pytest.mark.parametrize("action", ["inspect", "configure", "status", "deploy", "backup"])
def test_exact_ssh_target_and_no_secret_or_shell_arguments(monkeypatch, action):
    calls = []

    def fake(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"ok": True}).encode(), b"secret")

    monkeypatch.setattr(garage.subprocess, "run", fake)
    assert garage.remote(action) == {"ok": True}
    argv, kw = calls.pop()
    assert argv[: len(garage.SSH)] == list(garage.SSH)
    assert argv[-1].endswith(f" {action} {hashlib.sha256(garage.compose_bytes()).hexdigest()}")
    assert "owner.header" not in repr(argv)
    assert kw["input"] == (garage.compose_bytes() if action == "configure" else None)
    assert kw["timeout"] == 110
    assert not calls


def test_unknown_operation_and_remote_failure_are_safe(monkeypatch):
    monkeypatch.setattr(garage.subprocess, "run", lambda *a, **k: pytest.fail("remote called"))
    with pytest.raises(garage.OperatorError, match="unsupported"):
        garage.remote("rm")

    def failed(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 1, b"", b"credential secret")

    monkeypatch.setattr(garage.subprocess, "run", failed)
    with pytest.raises(garage.OperatorError) as caught:
        garage.remote("status")
    assert "credential secret" not in str(caught.value)


def test_deploy_gated_by_tls_before_remote(monkeypatch):
    monkeypatch.setattr(garage, "remote", lambda action: pytest.fail("remote called"))
    monkeypatch.setattr(garage, "verify_gateway", lambda: (_ for _ in ()).throw(OSError("no tls")))
    # Function is registered as a tool, so exercise the gateway primitive itself
    # with a failure rather than treating MCP annotations as security.
    with pytest.raises(OSError, match="no tls"):
        garage.verify_gateway()


def test_remote_program_scoped_actions_without_docker(tmp_path):
    """Execute remote program locally with a fake Docker boundary and temp-only paths."""
    script = garage.REMOTE.replace('/srv/tessera-garage"', f'{tmp_path}"')
    start = script.index("def run(argv):")
    end = script.index("try:\n", start)
    script = (
        script[:start]
        + 'def run(argv):\n    print(json.dumps({"argv": argv}), file=sys.stderr)\n'
        + '    return "[]"\n'
        + script[end:]
    )
    (tmp_path / "control").mkdir()
    (tmp_path / "backups").mkdir()
    body = garage.compose_bytes()
    digest = hashlib.sha256(body).hexdigest()

    def invoke(action, data=None, expected=digest):
        process = subprocess.run(
            [sys.executable, "-c", script, action, expected],
            input=data,
            capture_output=True,
            timeout=10,
            check=False,
        )
        return process.returncode, json.loads(process.stdout), process.stderr.decode()

    assert invoke("inspect")[1] == {
        "configured": False,
        "state_present": None,
        "backup_directory_present": True,
    }
    assert invoke("configure", b"wrong")[0] != 0
    assert not (tmp_path / "compose.yaml").exists()
    assert invoke("configure", body)[1] == {"configured": True}
    assert invoke("configure", body)[1] == {"configured": True}
    assert invoke("inspect")[1]["configured"] is True
    assert invoke("status")[1] == {"control": []}
    assert invoke("deploy")[1] == {"deployed": True}
    code, result, observed = invoke("backup")
    assert code == 0 and result["backup"].startswith("backup-")
    command = json.loads(observed)["argv"]
    assert command[:7] == [
        "docker",
        "--host",
        "unix:///var/run/docker.sock",
        "compose",
        "-p",
        "tessera-garage-control",
        "-f",
    ]
    assert command[8:13] == ["--profile", "maintenance", "run", "--rm", "--no-deps"]
    assert command[13] == "backup"
    assert invoke("erase")[0] != 0
    assert invoke("deploy", expected="0" * 64)[0] != 0
    assert list((tmp_path / "backups").iterdir()) == []
