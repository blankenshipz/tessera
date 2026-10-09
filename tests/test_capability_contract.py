"""Pure validation of declarations and the disabled-by-default worker contract."""

from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest
from pydantic import ValidationError
from test_capabilities import declaration

from tessera_control.capability_models import AdmitCapability, ArtifactAction, CapabilityDeclaration
from tessera_worker.capability_contract import (
    FenceEvidence,
    check_authority_window,
    check_broker_connection,
    check_download_bytes,
    check_lockfile_bytes,
    check_materialization,
    require_effect_authority,
)


def binding():
    policy = declaration()
    return AdmitCapability(
        execution_lease_id="lease-1",
        expected_epoch=1,
        environment_id=policy["environment_id"],
        image_digest=policy["image_digest"],
        action=ArtifactAction.model_validate(policy["actions"][0]),
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://artifacts.example.org/file",
        "https://127.0.0.1/file",
        "https://169.254.169.254/file",
        "https://10.0.0.1/file",
        "https://224.0.0.1/file",
        "https://example.local/file",
        "https://user:secret@artifacts.example.org/file",
        "https://artifacts.example.org/file?token=secret",
        "https://artifacts.example.org/file#fragment",
        "https://artifacts.example.org:0/file",
        "https://artifacts.example.org/a/../file",
        "https://artifacts.example.org/%2e%2e/file",
        "https://artifacts.example.org/file\\evil",
        "https://ARTIFACTS.example.org/file",
    ],
)
def test_forbidden_destinations_and_noncanonical_urls(url):
    with pytest.raises(ValidationError):
        ArtifactAction.model_validate(declaration()["actions"][0] | {"url": url})


@pytest.mark.parametrize(
    "root",
    [
        "/",
        "/host",
        "/control-pvc",
        "/state",
        "/var/run/secrets",
        "/workspace",
        "/workspace/artifacts/../host",
        "/workspace/artifacts//a",
        "/workspace/artifacts/\x00",
        "/workspace/artifacts-escape",
    ],
)
def test_no_host_control_or_ambient_credential_roots(root):
    with pytest.raises(ValidationError):
        ArtifactAction.model_validate(declaration()["actions"][0] | {"filesystem_root": root})


@pytest.mark.parametrize(
    "change",
    [
        {"network": []},
        {"model_registries": []},
        {"filesystem_roots": []},
        {"max_bytes": 1023},
        {"max_seconds": 9},
        {"max_bytes": True},
        {"network": [{"host": "artifacts.example.org", "port": 444}]},
        {"model_registries": ["https://artifacts.example.org/approve"]},
        {"allow_host": True},
        {"image_digest": "latest"},
    ],
)
def test_complete_explicit_declarations_required(change):
    with pytest.raises(ValidationError):
        CapabilityDeclaration.model_validate(declaration() | change)


def test_no_implicit_package_resolution_commands_or_credentials():
    policy = declaration("package")
    for change in (
        {"manager": "npm"},
        {"lockfile_sha256": None},
        {"channel": None},
        {"install_command": "curl x | sh"},
        {"credential_ref": "ambient"},
    ):
        with pytest.raises(ValidationError):
            CapabilityDeclaration.model_validate(
                policy | {"actions": [policy["actions"][0] | change]}
            )
    for change in ({"actions": policy["actions"] * 2}, {"package_channels": []}):
        with pytest.raises(ValidationError):
            CapabilityDeclaration.model_validate(policy | change)
    empty = CapabilityDeclaration(environment_id="sandbox", image_digest="sha256:" + "a" * 64)
    assert empty.actions == [] and empty.network == [] and empty.max_bytes == 0


def test_every_trusted_runtime_fence_and_identity_is_required():
    body = binding()
    missing = FenceEvidence(
        body.environment_id, body.image_digest, body.execution_lease_id, body.expected_epoch
    )
    with pytest.raises(ValueError):
        missing.require(body)
    flags = {field.name: True for field in fields(missing) if field.default is False}
    observed = replace(missing, **flags)
    observed.require(body)
    for flag in flags:
        with pytest.raises(ValueError):
            replace(observed, **{flag: False}).require(body)
    for change in (
        {"environment_id": "other"},
        {"image_digest": "sha256:" + "d" * 64},
        {"lease_id": "another"},
        {"epoch": 2},
    ):
        with pytest.raises(ValueError):
            replace(observed, **change).require(body)
    response = {
        "execution_enabled": False,
        "operation_id": "op",
        "binding": body.model_dump(mode="json"),
        "authority_until": (datetime.now(UTC) + timedelta(seconds=4)).isoformat(),
    }
    with pytest.raises(ValueError, match="disabled"):
        require_effect_authority(response, "op", body, observed)


@pytest.mark.parametrize(
    "ips",
    [
        [],
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["10.0.0.1"],
        ["::1"],
        ["224.0.0.1"],
        ["8.8.8.8", "192.168.1.1"],
    ],
)
def test_dns_rebinding_private_metadata_and_multicast_are_denied(ips):
    body = binding()
    with pytest.raises(ValueError):
        check_broker_connection(body, body.action.url, ips)


def test_redirect_path_escape_hash_lock_and_authority_checks():
    body = binding()
    check_broker_connection(body, body.action.url, ["8.8.8.8"])
    with pytest.raises(ValueError):
        check_broker_connection(body, "https://other.example.org/file", ["8.8.8.8"])
    check_materialization(body, "/workspace/artifacts/file")
    for path in ("/host/file", "/workspace/artifacts/../file", "/workspace/artifacts-escape/file"):
        with pytest.raises(ValueError):
            check_materialization(body, path)
    for seconds in (-1, 10):
        with pytest.raises(ValueError):
            check_authority_window((datetime.now(UTC) + timedelta(seconds=seconds)).isoformat())
    check_authority_window((datetime.now(UTC) + timedelta(seconds=4)).isoformat())
    content = b"offline synthetic bytes"
    action = body.action.model_copy(update={"sha256": sha256(content).hexdigest()})
    check_download_bytes(body.model_copy(update={"action": action}), content)
    with pytest.raises(ValueError):
        check_download_bytes(body, content)
    with pytest.raises(ValueError):
        check_download_bytes(
            body.model_copy(update={"action": action.model_copy(update={"max_bytes": 1})}), content
        )
    package = ArtifactAction.model_validate(
        declaration("package")["actions"][0] | {"lockfile_sha256": sha256(content).hexdigest()}
    )
    check_lockfile_bytes(body.model_copy(update={"action": package}), content)
    with pytest.raises(ValueError):
        check_lockfile_bytes(body.model_copy(update={"action": package}), b"changed lock")
