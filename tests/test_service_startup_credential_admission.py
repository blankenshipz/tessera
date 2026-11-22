"""The installer's files satisfy the reasoning service's existing start-up admission.

The start-up admission is the mapper in ``service_startup`` (forbidden environment, the
``model-credential`` reference file compared with the profile's ``credential_ref``, the API-key file
checks). The installer has no admission of its own; these tests drive its real output through the
real ``launch_spec`` and ``main`` entry points. Synthetic credentials only; nothing is started or
called.
"""

# ruff: noqa: F811
import io
import json
import os
import shutil

import pytest
from test_reasoning_credential_references import REF, anthropic  # noqa: F401

from tessera_agent import anthropic_messages, service_credentials
from tessera_agent.service_credentials import CredentialRefused
from tessera_agent.service_startup import StartupBlocked, launch_spec, main

SYNTHETIC_KEY = b"sk-ant-SYNTHETIC-" + b"k" * 40
OWNER, REFERENCE = (
    "owner1",
    "sentinel-reference-label",
)  # builds the REF used by the profile fixture
SERVICE, NAME = "tessera-reasoning", "anthropic-api-key"


def installed(tmp_path, profile, key=SYNTHETIC_KEY):
    """Run the real installer into a private store; return (store dir, receipt)."""
    store = tmp_path / "store"
    store.mkdir(mode=0o700)
    units = tmp_path / "units"
    units.mkdir()
    profile_file = tmp_path / "reasoning-profile.json"
    profile_file.write_text(json.dumps(profile))
    profile_file.chmod(0o600)
    receipt = service_credentials.install(
        SERVICE,
        NAME,
        io.BytesIO(key + b"\n"),
        credentials_dir=store,
        unit_dirs=[units],
        environ={},
        owner_label=OWNER,
        reference_label=REFERENCE,
        require_root=False,
        profile_path=profile_file,
    )
    return store, receipt


def manager_from(store, root, *, key_mode=0o600):
    """What systemd would hand the service: LoadCredential copies of the installed files."""
    shutil.copyfile(store / "reasoning-model-credential", root / "model-credential")
    (root / "model-credential").chmod(0o600)
    shutil.copyfile(store / "reasoning-anthropic-api-key", root / "anthropic-api-key")
    (root / "anthropic-api-key").chmod(key_mode)


def test_the_installed_files_satisfy_the_real_startup_admission(anthropic, tmp_path):  # noqa: F811
    root, profile, env = anthropic
    store, receipt = installed(tmp_path, profile)
    assert receipt["reference"] == REF
    manager_from(store, root)
    command, child = launch_spec("reasoning", tmp_path / "state", env)
    assert command[1:4] == ["-m", "tessera_agent.remote_reasoning_runtime", "run"]
    assert child["CREDENTIALS_DIRECTORY"] == str(root)
    assert not any(name.startswith(("ANTHROPIC_", "CLAUDE_")) for name in child)
    assert SYNTHETIC_KEY.decode() not in json.dumps(child) + " ".join(command)


def test_the_key_appears_only_in_the_key_file(anthropic, tmp_path):  # noqa: F811
    _, profile, _ = anthropic
    store, receipt = installed(tmp_path, profile)
    holders = [
        path.name
        for path in store.iterdir()
        if SYNTHETIC_KEY[:12] in path.read_bytes() or SYNTHETIC_KEY.decode() in path.name
    ]
    assert holders == ["reasoning-anthropic-api-key"]
    assert SYNTHETIC_KEY[:8].decode() not in json.dumps(receipt)
    assert oct(os.stat(store / "reasoning-anthropic-api-key").st_mode & 0o777) == "0o600"
    assert oct(os.stat(store / "reasoning-model-credential").st_mode & 0o777) == "0o600"


def test_a_profile_naming_a_different_reference_is_refused_at_start(anthropic, tmp_path):  # noqa: F811
    root, profile, env = anthropic
    store, _ = installed(tmp_path, profile)
    manager_from(store, root)
    other = {**profile, "credential_ref": "anthropic-api-key://owner1/another-reference"}
    (root / "profile").write_text(json.dumps(other))
    with pytest.raises(StartupBlocked, match="model_credential_reference_mismatch"):
        launch_spec("reasoning", tmp_path / "state", env)


def test_the_installer_refuses_when_the_profile_names_a_different_reference(anthropic, tmp_path):  # noqa: F811
    _, profile, _ = anthropic
    other = {**profile, "credential_ref": "anthropic-api-key://owner1/another-reference"}
    store = tmp_path / "store"
    store.mkdir(mode=0o700)
    profile_file = tmp_path / "other-profile.json"
    profile_file.write_text(json.dumps(other))
    profile_file.chmod(0o600)
    with pytest.raises(CredentialRefused) as refused:
        service_credentials.install(
            SERVICE,
            NAME,
            io.BytesIO(SYNTHETIC_KEY + b"\n"),
            credentials_dir=store,
            unit_dirs=[],
            environ={},
            owner_label=OWNER,
            reference_label=REFERENCE,
            require_root=False,
            profile_path=profile_file,
        )
    assert refused.value.code == "reference_mismatch"
    assert list(store.iterdir()) == []  # nothing written, no receipt
    assert SYNTHETIC_KEY.decode() not in str(refused.value)


def test_a_key_file_other_than_private_is_refused_at_start(anthropic, tmp_path):  # noqa: F811
    root, profile, env = anthropic
    store, _ = installed(tmp_path, profile)
    manager_from(store, root, key_mode=0o640)
    with pytest.raises(StartupBlocked, match="missing_or_invalid_model_api_key_credential"):
        launch_spec("reasoning", tmp_path / "state", env)


def test_a_missing_key_file_is_refused_at_start(anthropic, tmp_path):  # noqa: F811
    root, profile, env = anthropic
    store, _ = installed(tmp_path, profile)
    manager_from(store, root)
    (root / "anthropic-api-key").unlink()
    with pytest.raises(StartupBlocked, match="missing_or_invalid_model_api_key_credential"):
        launch_spec("reasoning", tmp_path / "state", env)


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"])
@pytest.mark.parametrize("value", ["", SYNTHETIC_KEY.decode()])
def test_a_general_key_variable_blocks_start_even_with_a_valid_install(  # noqa: F811
    anthropic, tmp_path, name, value
):
    root, profile, env = anthropic
    store, _ = installed(tmp_path, profile)
    manager_from(store, root)
    with pytest.raises(StartupBlocked, match="api_key_environment_present") as blocked:
        launch_spec("reasoning", tmp_path / "state", {**env, name: value})
    assert SYNTHETIC_KEY.decode() not in str(blocked.value)


def test_main_exits_nonzero_without_echoing_the_key(anthropic, tmp_path, monkeypatch, capsys):  # noqa: F811
    root, profile, env = anthropic
    store, _ = installed(tmp_path, profile)
    manager_from(store, root)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", env["CREDENTIALS_DIRECTORY"])
    monkeypatch.setenv("ANTHROPIC_API_KEY", SYNTHETIC_KEY.decode())
    assert main(["reasoning", "--state-dir", str(tmp_path / "state")]) == 78
    captured = capsys.readouterr()
    assert SYNTHETIC_KEY.decode() not in captured.out + captured.err


def test_a_key_the_runtime_would_reject_is_refused_by_the_installer(tmp_path):
    too_long = b"sk-ant-SYNTHETIC-" + b"k" * (513 - len("sk-ant-SYNTHETIC-"))
    assert len(too_long) == 513
    assert anthropic_messages._KEY.fullmatch(too_long) is None  # the runtime refuses it
    assert anthropic_messages._KEY.fullmatch(too_long[:512]) is not None  # and takes 512
    store = tmp_path / "store"
    store.mkdir(mode=0o700)
    with pytest.raises(CredentialRefused) as refused:
        service_credentials.install(
            SERVICE,
            NAME,
            io.BytesIO(too_long + b"\n"),
            credentials_dir=store,
            unit_dirs=[],
            environ={},
            owner_label=OWNER,
            reference_label=REFERENCE,
            require_root=False,
        )
    assert refused.value.code == "invalid_credential_shape"
    assert too_long.decode() not in str(refused.value)
    assert list(store.iterdir()) == []
