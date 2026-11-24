"""The reasoning runtime's own entrypoint refuses to run when a general API-key variable is set.

The launcher already refuses (test_service_startup_credential_admission); this pins the same
fail-closed rule on the ``tessera-reasoning run`` entrypoint itself, so a direct start cannot
proceed with a raw key in the environment. Fakes only: no key is real, nothing is sent.
"""

import json

import pytest

import tessera_agent.remote_reasoning_runtime as rr
from tessera_agent.model_destinations import ANTHROPIC_ACCOUNT_MODE, DestinationRefused

FAKE_VALUE = "fake-not-a-real-key-0000"


@pytest.fixture
def stub_profile(monkeypatch, tmp_path):
    profile = {"model": {"providerID": "anthropic"}, "account_mode": ANTHROPIC_ACCOUNT_MODE}
    monkeypatch.setattr(rr, "private_file", lambda path, **_: json.dumps(profile).encode())
    monkeypatch.setattr(rr, "validate_profile", lambda value: None)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"])
@pytest.mark.parametrize("value", ["", FAKE_VALUE])
def test_run_refuses_a_general_key_variable_before_anything_else(
    stub_profile, monkeypatch, capsys, name, value
):
    monkeypatch.setenv(name, value)
    args = ["run", "--profile", "p", "--state-dir", str(stub_profile / "state")]
    with pytest.raises(DestinationRefused, match="api_key_environment_present"):
        rr.main(args)
    assert FAKE_VALUE not in "".join(capsys.readouterr())
    assert not (stub_profile / "state").exists()  # no state was created


def test_run_without_the_variable_reaches_the_credential_reference_check(stub_profile, capsys):
    # Without a key variable the next gate is the manager credential directory (a reference),
    # which main reports as a configuration exit (never restarted).
    args = ["run", "--profile", "p", "--state-dir", str(stub_profile / "state")]
    assert rr.main(args) == rr.EXIT_CONFIG
    assert "missing_manager_credential_directory" in capsys.readouterr().err
