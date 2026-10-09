"""Static service profiles and synthetic credentials only; no services/inference."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from tessera_agent.remote_reasoning_store import ReasoningStore
from tessera_agent.service_startup import StartupBlocked, launch_spec, main

DEPLOY = Path(__file__).resolve().parents[1] / "deploy" / "remote-agent-platform"


def write_private(path, content):
    path.write_text(content)
    path.chmod(0o600)


@pytest.fixture
def manager(tmp_path):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    profile = {
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "grant1",
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "included-chatgpt-oauth",
        "model": {"providerID": "openai", "id": "model1"},
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "agent_token_env": "TESSERA_REASONING_AGENT_TOKEN",
        "harness_token_env": "TESSERA_REASONING_HARNESS_TOKEN",
    }
    write_private(root / "profile", json.dumps(profile))
    write_private(root / "agent-token", "synthetic-agent\n")
    write_private(root / "harness-password", "synthetic-harness\n")
    write_private(root / "prompt", "synthetic goal")
    env = {
        "CREDENTIALS_DIRECTORY": str(root),
        "PATH": "/usr/bin",
        "HOME": "/service",
        "OPENAI_API_KEY": "must-not-forward",
        "PYTHONPATH": "/untrusted",
        "LD_PRELOAD": "/untrusted",
        "ARBITRARY": "must-not-forward",
    }
    return root, profile, env


def test_fixed_reasoning_command_maps_only_named_credentials(manager, tmp_path):
    root, _, env = manager
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert command[1:4] == ["-m", "tessera_agent.remote_reasoning_runtime", "run"]
    assert command[-2:] == ["--prompt-file", str(root / "prompt")]
    assert child["TESSERA_REASONING_AGENT_TOKEN"] == "synthetic-agent"
    assert child["TESSERA_REASONING_HARNESS_TOKEN"] == "synthetic-harness"
    assert child["PYTHONNOUSERSITE"] == "1"
    assert set(child) == {
        "PATH",
        "HOME",
        "PYTHONNOUSERSITE",
        "TESSERA_REASONING_AGENT_TOKEN",
        "TESSERA_REASONING_HARNESS_TOKEN",
    }
    assert all(
        "synthetic-agent" not in value and "synthetic-harness" not in value for value in command
    )


def test_a2a_fixed_loopback_module_and_state(manager, tmp_path):
    root, _, env = manager
    state = tmp_path / "peer-state"
    write_private(
        root / "profile",
        json.dumps(
            {
                "database": str(state / "peer.sqlite3"),
                "control_token_env": "TESSERA_A2A_CONTROL_TOKEN",
            }
        ),
    )
    command, child = launch_spec("a2a", state, env)
    assert command[1:3] == ["-m", "tessera_control.a2a_peer"]
    assert command[-4:] == ["--host", "127.0.0.1", "--port", "8771"]
    assert child["TESSERA_A2A_CONTROL_TOKEN"] == "synthetic-agent"
    assert "TESSERA_REASONING_HARNESS_TOKEN" not in child


@pytest.mark.parametrize("name", ["shell", "control", "/bin/sh", "reasoning-stop", "a2a;id"])
def test_no_generic_executable_proxy(manager, tmp_path, name):
    _, _, env = manager
    with pytest.raises(StartupBlocked):
        launch_spec(name, tmp_path / "state", env)


def test_missing_manager_directory_and_relative_state_rejected(manager):
    _, _, env = manager
    with pytest.raises(StartupBlocked):
        launch_spec("reasoning", Path("relative"), env)
    with pytest.raises(StartupBlocked):
        launch_spec("reasoning", Path("/state"), {})


@pytest.mark.parametrize("field", ["agent_token_env", "harness_token_env"])
def test_arbitrary_secret_environment_mapping_denied(manager, tmp_path, field):
    root, profile, env = manager
    profile[field] = "OPENAI_API_KEY"
    write_private(root / "profile", json.dumps(profile))
    with pytest.raises(StartupBlocked):
        launch_spec("reasoning", tmp_path / "state", env)


@pytest.mark.parametrize("bad", ["synthetic\r\nheader", "synthetic token", "", "x" * 4097])
def test_invalid_credential_bytes_fail_closed(manager, tmp_path, bad):
    root, _, env = manager
    write_private(root / "agent-token", bad)
    with pytest.raises(StartupBlocked):
        launch_spec("reasoning", tmp_path / "state", env)


@pytest.mark.parametrize("name", ["profile", "agent-token", "harness-password", "prompt"])
def test_public_or_symlink_reference_denied(manager, tmp_path, name):
    root, _, env = manager
    path = root / name
    path.chmod(0o644)
    with pytest.raises(ValueError):
        launch_spec("reasoning", tmp_path / "state", env)
    path.chmod(0o600)
    target = root / (name + "-target")
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(OSError):
        launch_spec("reasoning", tmp_path / "state", env)


def test_a2a_cannot_write_ledger_outside_state(manager, tmp_path):
    root, _, env = manager
    write_private(
        root / "profile",
        json.dumps(
            {
                "database": "/unapproved/peer.sqlite3",
                "control_token_env": "TESSERA_A2A_CONTROL_TOKEN",
            }
        ),
    )
    with pytest.raises(StartupBlocked):
        launch_spec("a2a", tmp_path / "state", env)


@pytest.mark.parametrize("port", [True, "8772", 80, 65536])
def test_a2a_port_reference_cannot_widen_listener_or_inject_arguments(manager, tmp_path, port):
    root, _, env = manager
    state = tmp_path / "state"
    write_private(
        root / "profile",
        json.dumps(
            {
                "database": str(state / "peer.sqlite3"),
                "control_token_env": "TESSERA_A2A_CONTROL_TOKEN",
                "listen_port": port,
            }
        ),
    )
    with pytest.raises(StartupBlocked):
        launch_spec("a2a", state, env)


def test_separate_peer_instance_can_select_approved_unprivileged_port(manager, tmp_path):
    root, _, env = manager
    state = tmp_path / "peer-two"
    write_private(
        root / "profile",
        json.dumps(
            {
                "database": str(state / "peer.sqlite3"),
                "control_token_env": "TESSERA_A2A_CONTROL_TOKEN",
                "listen_port": 8772,
            }
        ),
    )
    command, _ = launch_spec("a2a", state, env)
    assert command[-4:] == ["--host", "127.0.0.1", "--port", "8772"]


def test_fifo_credential_denied_without_waiting_for_a_writer(manager, tmp_path):
    root, _, env = manager
    (root / "agent-token").unlink()
    os.mkfifo(root / "agent-token", 0o600)
    with pytest.raises(ValueError, match="regular files"):
        launch_spec("reasoning", tmp_path / "state", env)


def test_execstop_persists_stop_without_tokens_or_harness(manager, tmp_path, monkeypatch):
    root, _, env = manager
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    (root / "agent-token").unlink()
    (root / "harness-password").unlink()
    state = tmp_path / "state"
    store = ReasoningStore(state)
    store.bind("run1", {"prompt_sha256": hashlib.sha256(b"synthetic goal").hexdigest()}, 100)
    store.close()
    assert main(["reasoning-stop", "--state-dir", str(state)]) == 0
    resumed = ReasoningStore(state)
    assert resumed.get("run1")["stop_requested"] == 1
    resumed.close()


def test_startup_errors_never_echo_secret_contents(manager, tmp_path, monkeypatch, capsys):
    root, _, env = manager
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    write_private(root / "profile", "synthetic-sensitive-input invalid json")
    assert main(["reasoning", "--state-dir", str(tmp_path / "state")]) == 78
    captured = capsys.readouterr()
    assert "synthetic-sensitive-input" not in captured.err
    assert "startup blocked" in captured.err
    assert not captured.out


@pytest.mark.parametrize("unit", ["tessera-control", "tessera-reasoning", "tessera-a2a-peer"])
def test_units_are_hardened_installed_profiles(unit):
    text = (DEPLOY / (unit + ".service")).read_text()
    for directive in (
        "UMask=0077",
        "StateDirectoryMode=0700",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=yes",
        "PrivateDevices=yes",
        "CapabilityBoundingSet=",
        "KillMode=control-group",
    ):
        assert directive in text
    assert "ExecStart=/opt/tessera/venv/bin/" in text
    assert "Environment=" not in text
    assert "/bin/sh" not in text and "/bin/bash" not in text
    assert "0.0.0.0" not in text
    if unit != "tessera-control":
        assert "WantedBy=" not in "\n".join(
            line for line in text.splitlines() if not line.startswith("#")
        )
        assert "ConditionPathExists=/etc/tessera/" in text
        assert "LoadCredential=profile:" in text
        assert "RestartPreventExitStatus=" in text
    else:
        assert "--bind-host 127.0.0.1" in text


def test_docs_and_peer_profile_match_installed_extra_and_safe_references():
    profile = json.loads((DEPLOY / "a2a-peer.example.json").read_text())
    assert profile["control_token_env"] == "TESSERA_A2A_CONTROL_TOKEN"
    assert profile["database"] == "/var/lib/tessera-a2a/peer.sqlite3"
    assert profile["public_url"] == "http://127.0.0.1:8771"
    text = (DEPLOY / "README.md").read_text()
    assert "a2a-sdk[http-server]==1.2.1" in text
    assert "not provider termination" in text
    assert "systemd-analyze" in text
    for relative in (
        "../../docs/remote-reasoning-runtime.md",
        "../kubernetes/remote-dispatch-worker.example.yaml",
    ):
        assert (DEPLOY / relative).is_file()
