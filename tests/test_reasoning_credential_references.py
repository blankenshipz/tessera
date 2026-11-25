"""Synthetic Anthropic API-key-reference startup checks; no network or model calls."""

import json
from pathlib import Path

import pytest

from tessera_agent.model_destinations import FORBIDDEN_ENVIRONMENT
from tessera_agent.service_startup import StartupBlocked, launch_spec, main

SENTINEL = "sentinel-api-key-value"
API_KEY = "sk-ant-SENTINEL-API-KEY-VALUE"
REF_LABEL = "sentinel-reference-label"
REF = f"anthropic-api-key://owner1/{REF_LABEL}"


def write_private(path, content):
    path.write_text(content)
    path.chmod(0o600)


@pytest.fixture
def anthropic(tmp_path):
    root = tmp_path / "credentials"
    root.mkdir(mode=0o700)
    profile = {
        "run_id": "run1",
        "agent": "researcher",
        "grant_id": "grant1",
        "subject_credential_id": "agent1",
        "destination": "lab",
        "account_mode": "anthropic-api-key-ref",
        "credential_ref": REF,
        "model": {"providerID": "anthropic", "id": "claude-sonnet-x"},
        "max_steps": 3,
        "max_tokens": 100,
        "max_duration_seconds": 60,
        "permissions": [{"action": "*", "resource": "*", "effect": "deny"}],
        "limits": {
            "max_output_tokens_per_call": 64,
            "max_input_tokens_per_call": 1000,
            "max_cost_cents_per_call": 10,
            "daily_token_cap": 10000,
            "daily_cost_cap_cents": 100,
        },
        "pricing_cents_per_mtok": {
            "input": 300,
            "output": 1500,
            "cache_read": 30,
            "cache_write": 375,
        },
        "agent_token_env": "TESSERA_REASONING_AGENT_TOKEN",
    }
    write_private(root / "profile", json.dumps(profile))
    write_private(root / "agent-token", "synthetic-agent\n")
    write_private(root / "prompt", "synthetic goal")
    write_private(root / "model-credential", REF + "\n")
    write_private(root / "anthropic-api-key", API_KEY + "\n")
    env = {"CREDENTIALS_DIRECTORY": str(root), "PATH": "/usr/bin", "HOME": "/service"}
    return root, profile, env


def test_anthropic_reference_resolves_without_forwarding_content(anthropic, tmp_path):
    _, _, env = anthropic
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    # No harness password/token: the direct Messages API runtime has no harness.
    assert set(child) == {
        "PATH",
        "HOME",
        "PYTHONNOUSERSITE",
        "TESSERA_REASONING_AGENT_TOKEN",
        "CREDENTIALS_DIRECTORY",
    }
    assert not any(name.startswith(("ANTHROPIC_", "CLAUDE_")) for name in child)
    for secret in (SENTINEL, API_KEY, "SENTINEL-API-KEY", REF_LABEL):
        assert secret not in json.dumps([command, child])


@pytest.mark.parametrize("name", FORBIDDEN_ENVIRONMENT)
def test_forbidden_environment_refused_for_anthropic_and_openai(anthropic, tmp_path, name):
    root, profile, env = anthropic
    with pytest.raises(StartupBlocked, match="api_key_environment_present") as caught:
        launch_spec("reasoning", tmp_path / "state", {**env, name: SENTINEL})
    assert SENTINEL not in str(caught.value)
    # OpenAI profile is refused too.
    profile.update(account_mode="included-chatgpt-oauth", model={"providerID": "openai", "id": "m"})
    profile.update(agent="researcher", harness_token_env="TESSERA_REASONING_HARNESS_TOKEN")
    for field in ("credential_ref", "limits", "pricing_cents_per_mtok"):
        profile.pop(field)
    write_private(root / "profile", json.dumps(profile))
    with pytest.raises(StartupBlocked, match="api_key_environment_present"):
        launch_spec("reasoning", tmp_path / "state", {**env, name: SENTINEL})


@pytest.mark.parametrize("mode", ["reasoning", "reasoning-stop", "research"])
def test_main_refuses_api_key_before_reading_profile(
    anthropic, tmp_path, monkeypatch, capsys, mode
):
    root, _, env = anthropic
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    (root / "profile").unlink()  # would otherwise fail differently; refusal comes first
    assert main([mode, "--state-dir", str(tmp_path / "state")]) == 78
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err + captured.out
    assert "startup blocked" in captured.err


@pytest.mark.parametrize("damage", ["missing", "empty", "public", "blank"])
def test_missing_or_bad_reference_file_refused(anthropic, tmp_path, damage):
    root, _, env = anthropic
    path = root / "model-credential"
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        write_private(path, "")
    elif damage == "blank":
        write_private(path, " \n")
    else:
        path.chmod(0o644)
    with pytest.raises(StartupBlocked, match="model_credential_reference") as caught:
        launch_spec("reasoning", tmp_path / "state", env)
    assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize(
    "ref", ["sk-ant-" + SENTINEL, "anthropic-api-key://owner1", "claude-subscription://a/b", None]
)
def test_invalid_reference_in_profile_refused_without_echo(anthropic, tmp_path, ref):
    root, profile, env = anthropic
    if ref is None:
        profile.pop("credential_ref")
    else:
        profile["credential_ref"] = ref
    write_private(root / "profile", json.dumps(profile))
    with pytest.raises(ValueError) as caught:
        launch_spec("reasoning", tmp_path / "state", env)
    assert SENTINEL not in str(caught.value)


def test_secret_never_in_stderr_on_blocked_start(anthropic, tmp_path, monkeypatch, capsys):
    root, _, env = anthropic
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    (root / "model-credential").chmod(0o644)
    assert main(["reasoning", "--state-dir", str(tmp_path / "state")]) == 78
    captured = capsys.readouterr()
    assert SENTINEL not in captured.err + captured.out


DEPLOY = Path(__file__).resolve().parents[1] / "deploy/remote-agent-platform"
MODEL_LINE = "LoadCredential=model-credential:/etc/tessera/credentials/reasoning-model-credential"


def active_lines(path):
    return [
        x.strip()
        for x in path.read_text().splitlines()
        if x.strip() and not x.lstrip().startswith("#")
    ]


def load_credentials(*unit_files):
    """Simulate systemd: an empty LoadCredential= clears the list, later lines append."""
    creds = []
    for path in unit_files:
        for line in active_lines(path):
            if not line.startswith("LoadCredential="):
                continue
            value = line.split("=", 1)[1]
            if value:
                creds.append(value.split(":", 1)[0])
            else:
                creds.clear()
    return creds


def base_unit():
    return DEPLOY / "tessera-reasoning.service"


def openai_drop_in():
    return DEPLOY / "tessera-reasoning.service.d/openai.conf.example"


def test_base_unit_defaults_to_anthropic_reference():
    unit = base_unit().read_text()
    assert MODEL_LINE in active_lines(base_unit())
    assert "\n[Install]" not in unit
    assert "ConditionPathExists=/etc/tessera/reasoning.admitted" in unit
    assert "UnsetEnvironment" not in unit


def test_old_anthropic_drop_in_is_gone():
    assert not (DEPLOY / "tessera-reasoning.service.d/anthropic.conf.example").exists()


BASE_CREDENTIALS = [
    "LoadCredential=profile:/etc/tessera/reasoning.json",
    "LoadCredential=prompt:/etc/tessera/reasoning-prompt.txt",
    "LoadCredential=agent-token:/etc/tessera/credentials/reasoning-agent-token",
    # The admission marker, by reference (ADR 0170); start-up refuses a malformed or expired one.
    "LoadCredential=admission:/etc/tessera/reasoning.admitted",
    MODEL_LINE,
    "LoadCredential=anthropic-api-key:/etc/tessera/credentials/reasoning-anthropic-api-key",
]
OPENAI_CREDENTIALS = [
    *BASE_CREDENTIALS[:3],  # profile, prompt, agent-token (the drop-in does not reload admission)
    "LoadCredential=harness-password:/etc/tessera/credentials/reasoning-harness-password",
]


def test_base_unit_loads_exactly_the_anthropic_api_key_references():
    lines = [x for x in active_lines(base_unit()) if x.startswith("LoadCredential=")]
    assert lines == BASE_CREDENTIALS
    assert not any("harness-password" in x for x in active_lines(base_unit()))


def test_openai_drop_in_resets_and_readds_only_the_four_openai_references():
    lines = active_lines(openai_drop_in())
    assert lines == ["[Service]", "LoadCredential=", *OPENAI_CREDENTIALS]
    assert not any("model-credential" in x or "anthropic-api-key" in x for x in lines)
    assert not any(x.startswith("[Install]") or "UnsetEnvironment" in x for x in lines)


def test_effective_credential_set_with_and_without_openai_drop_in():
    with_drop_in = load_credentials(base_unit(), openai_drop_in())
    assert sorted(with_drop_in) == sorted({"profile", "prompt", "agent-token", "harness-password"})
    assert len(with_drop_in) == 4
    default = load_credentials(base_unit())
    assert sorted(default) == sorted(
        {"profile", "prompt", "agent-token", "admission", "model-credential", "anthropic-api-key"}
    )
    assert len(default) == 6


@pytest.mark.parametrize(
    "content",
    [
        "anthropic-api-key://owner1/other\n",
        REF + "\n\n",
        REF + " \n",
        REF + "\r\n",
        REF[:-1] + "\n",
        REF + "\x00\n",
        REF + "\u00e9\n",
    ],
)
def test_reference_file_must_equal_profile_reference(
    anthropic, tmp_path, monkeypatch, capsys, content
):
    root, _, env = anthropic
    write_private(root / "model-credential", content)
    with pytest.raises(StartupBlocked) as caught:
        launch_spec("reasoning", tmp_path / "state", env)
    assert str(caught.value) == "model_credential_reference_mismatch"
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    assert main(["reasoning", "--state-dir", str(tmp_path / "state")]) == 78
    captured = capsys.readouterr()
    assert REF_LABEL not in captured.err + captured.out + str(caught.value)
    assert "owner1" not in captured.err + captured.out


def test_reference_file_without_trailing_newline_accepted(anthropic, tmp_path):
    root, _, env = anthropic
    write_private(root / "model-credential", REF)
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert REF_LABEL not in json.dumps([command, child])


@pytest.mark.parametrize("damage", ["missing", "empty", "public", "symlink", "directory"])
def test_api_key_file_must_be_private_non_empty_regular(anthropic, tmp_path, damage):
    root, _, env = anthropic
    path = root / "anthropic-api-key"
    if damage == "missing":
        path.unlink()
    elif damage == "empty":
        write_private(path, "")
    elif damage == "public":
        path.chmod(0o644)
    elif damage == "symlink":
        target = tmp_path / "elsewhere"
        write_private(target, API_KEY)
        path.unlink()
        path.symlink_to(target)
    else:
        path.unlink()
        path.mkdir(mode=0o700)
    with pytest.raises(StartupBlocked, match="missing_or_invalid_model_api_key_credential") as c:
        launch_spec("reasoning", tmp_path / "state", env)
    assert API_KEY not in str(c.value)


def test_anthropic_start_needs_no_harness_password(anthropic, tmp_path):
    root, _, env = anthropic
    assert not (root / "harness-password").exists()
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert "TESSERA_REASONING_HARNESS_TOKEN" not in child
    assert child["CREDENTIALS_DIRECTORY"] == str(root)


def test_api_key_never_in_child_env_argv_or_stderr(anthropic, tmp_path, monkeypatch, capsys):
    root, _, env = anthropic
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert API_KEY not in json.dumps([command, child])
    assert all("anthropic-api-key" not in part for part in command)
    seen = {}

    def fake_execve(path, argv, environment):
        seen.update(argv=argv, env=environment)
        return 0

    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    monkeypatch.setattr("tessera_agent.service_startup.os.execve", fake_execve)
    main(["reasoning", "--state-dir", str(tmp_path / "state")])
    assert API_KEY not in json.dumps(seen)
    (root / "anthropic-api-key").chmod(0o644)
    assert main(["reasoning", "--state-dir", str(tmp_path / "state")]) == 78
    captured = capsys.readouterr()
    assert API_KEY not in captured.err + captured.out


def test_openai_profile_still_requires_harness_password(anthropic, tmp_path):
    root, profile, env = anthropic
    profile.update(
        account_mode="included-chatgpt-oauth",
        model={"providerID": "openai", "id": "m"},
        agent="researcher",
        harness_token_env="TESSERA_REASONING_HARNESS_TOKEN",
    )
    for field in ("credential_ref", "limits", "pricing_cents_per_mtok"):
        profile.pop(field)
    write_private(root / "profile", json.dumps(profile))
    with pytest.raises(OSError):
        launch_spec("reasoning", tmp_path / "state", env)
    write_private(root / "harness-password", "synthetic-harness\n")
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert child["TESSERA_REASONING_HARNESS_TOKEN"] == "synthetic-harness"
    assert "CREDENTIALS_DIRECTORY" not in child
