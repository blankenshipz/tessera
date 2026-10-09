"""Actual temporary HTTPS/Store; synthetic owner/private facts, zero providers."""

import argparse
import hashlib
import json
import subprocess
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from test_runtime_binding_client import build_local_api_runtime

from tessera_mcp import runtime_setup as setup
from tessera_mcp.runtime_setup_models import SetupProfile


def private_json(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return str(path.resolve())


@pytest.fixture
def configured(tmp_path):
    with contextmanager(build_local_api_runtime)(tmp_path) as data:
        # TEST-ONLY independent installed-process/signature approval callback. It
        # cannot satisfy live installed acceptance. Actual HTTPS and DB are real.
        readback = {
            "manifest_sha256": hashlib.sha256(b"test-signed-public-manifest").hexdigest(),
            "signature_reference": "test-signature-only",
            "approval_reference": "independent-test-approval",
            "process_link_reference": "test-current-process-link",
            "publisher_ref": data.enrollment.scope.publisher_ref,
            "opaque_owner_ref": data.enrollment.scope.opaque_owner_ref,
            "session_ref": data.enrollment.scope.session_ref,
            "pins": data.enrollment.pins.model_dump(),
            "subject_credential_id": data.enrollment_body["subject_credential_id"],
            "publisher_credential_id": data.enrollment_body["publisher_credential_id"],
        }
        manifest = tmp_path / "public-manifest.json"
        manifest.write_bytes(b"test-signed-public-manifest")
        resource = tmp_path / "owner-resource-reference.json"
        private_json(resource, {"test_only": True})
        adapter = tmp_path / "trusted-test-owner.py"
        adapter.write_text(
            "from tessera_mcp.runtime_setup_models import (\n"
            "    InstalledOwnerCapability, InstalledReadback)\n"
            "def open_installed_owner(binding, *, credential_references):\n"
            f"    readback = InstalledReadback.model_validate({readback!r})\n"
            "    def assert_current():\n"
            "        # Synthetic independent test process/approval, NOT production.\n"
            "        return None\n"
            "    return InstalledOwnerCapability(readback, assert_current)\n"
        )

        def token(value):
            return (
                value if isinstance(value, str) else value["Authorization"].removeprefix("Bearer ")
            )

        human = tmp_path / "human-token"
        subject = tmp_path / "subject-token"
        publisher = tmp_path / "publisher-token"
        for path, value in (
            (human, data.human),
            (subject, data.subject),
            (publisher, data.publisher),
        ):
            path.write_text(token(value))
            path.chmod(0o600)
        profile = {
            "version": "tessera-runtime-setup-v1",
            "origin": data.client.origin,
            "audience": "control",
            "destination": data.client.destination,
            "project_id": data.project,
            "human_credential_file": str(human.resolve()),
            "subject_credential_file": str(subject.resolve()),
            "publisher_credential_file": str(publisher.resolve()),
            "ca_file": str(data.cert.resolve()),
            "owner": {
                "adapter_file": str(adapter.resolve()),
                "adapter_sha256": hashlib.sha256(adapter.read_bytes()).hexdigest(),
                "resource_reference_file": str(resource.resolve()),
                "signed_manifest_file": str(manifest.resolve()),
                "approved_manifest_sha256": readback["manifest_sha256"],
                "approval_reference": readback["approval_reference"],
            },
            "policy": data.policy.model_dump(mode="json"),
            "enrollment_request": data.enrollment_body,
            "resolve_request": data.resolve,
        }
        file = tmp_path / "setup-profile.json"
        private_json(file, profile)
        yield data, file, profile


def invoke(file, command, **kwargs):
    return setup.run(argparse.Namespace(profile=str(file.resolve()), command=command, **kwargs))


def test_actual_https_setup_workflow(configured, tmp_path):
    data, file, profile = configured
    prepared = tmp_path / "prepared.json"
    assert (
        invoke(file, "prepare-profile", output=str(prepared))["status"] == "prepared-not-enrolled"
    )
    assert prepared.stat().st_mode & 0o077 == 0
    enrolled = invoke(file, "enroll", key="setup-enroll")
    finance = {**data.financial_body, "enrollment_ref": enrolled["enrollment_ref"]}
    input_file = tmp_path / "operator-observed-financial.json"
    private_json(input_file, finance)
    issued = invoke(file, "operator-financial-ref", key="setup-financial", input=str(input_file))
    assert issued["observed_at"] == data.financial.model_dump(mode="json")["observed_at"]
    profile["resolve_request"]["enrollment_ref"] = enrolled["enrollment_ref"]
    profile["resolve_request"]["financial_ref"] = issued["attestation_ref"]
    private_json(file, profile)
    resolved = invoke(file, "doctor", key="setup-doctor")
    assert resolved["provider_entitlement"] == "unknown"
    assert resolved["pins"] == profile["enrollment_request"]["pins"]
    assert invoke(file, "current-resolve", key="setup-resolve")["scope"] == resolved["scope"]
    invoke(
        file,
        "revoke",
        kind="financial",
        ref=issued["attestation_ref"],
        revision=1,
        key="setup-revoke-financial",
    )
    with pytest.raises(setup.SetupDenied):
        invoke(file, "doctor", key="setup-revoked")
    assert not data.marker.exists()


@pytest.mark.parametrize(
    "fault",
    [
        "unenrolled",
        "foreign-project",
        "manifest-tamper",
        "adapter-tamper",
        "missing-owner",
        "same-token",
        "credential-symlink",
        "public-symlink",
        "credential-public",
        "unknown-owner-json",
    ],
)
def test_setup_faults_fail_closed(configured, fault, tmp_path):
    data, file, profile = configured
    if fault == "unenrolled":
        profile["resolve_request"] = None
    elif fault == "foreign-project":
        profile["project_id"] = "foreign-project"
    elif fault == "manifest-tamper":
        Path(profile["owner"]["signed_manifest_file"]).write_bytes(b"tampered")
    elif fault == "adapter-tamper":
        Path(profile["owner"]["adapter_file"]).write_text("raise Exception('untrusted')")
    elif fault == "missing-owner":
        profile["owner"]["adapter_file"] = str(tmp_path / "absent.py")
    elif fault == "same-token":
        Path(profile["publisher_credential_file"]).write_bytes(
            Path(profile["human_credential_file"]).read_bytes()
        )
    elif fault in {"credential-symlink", "public-symlink"}:
        link = tmp_path / "linked"
        field = (
            "publisher_credential_file" if fault == "credential-symlink" else "signed_manifest_file"
        )
        mapping = profile if fault == "credential-symlink" else profile["owner"]
        link.symlink_to(mapping[field])
        mapping[field] = str(link)
    elif fault == "credential-public":
        Path(profile["publisher_credential_file"]).chmod(0o644)
    else:
        adapter = Path(profile["owner"]["adapter_file"])
        adapter.write_text("def open_installed_owner(binding, **kw): return {'complete': True}\n")
        profile["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
    private_json(file, profile)
    with pytest.raises((setup.SetupDenied, OSError)):
        invoke(file, "doctor", key="setup-fault")
    assert not data.marker.exists()


def test_policy_bootstrap_requires_explicit_choices(configured, tmp_path):
    data, file, profile = configured
    profile["policy"] = None
    profile["enrollment_request"] = None
    profile["resolve_request"] = None
    private_json(file, profile)
    body = tmp_path / "human-policy.json"
    private_json(body, data.policy_body)
    result = invoke(file, "issue-policy", key="new-human-policy", input=str(body))
    assert result["operator_issuer"] == data.policy.operator_issuer
    assert result["policy"] == data.policy.model_dump(mode="json")["policy"]
    with pytest.raises(setup.SetupDenied):
        invoke(file, "prepare-profile", output=str(tmp_path / "cannot.json"))


def test_financial_never_supplies_defaults(configured, tmp_path):
    _, file, _ = configured
    body = tmp_path / "empty-financial.json"
    private_json(body, {})
    with pytest.raises(ValidationError):
        invoke(file, "operator-financial-ref", input=str(body), key="no-defaults")


def test_cli_diagnostic_does_not_print_secret(configured, capsys):
    _, file, profile = configured
    token = Path(profile["human_credential_file"]).read_text()
    profile["owner"]["adapter_file"] = "/not/supplied/owner.py"
    private_json(file, profile)
    assert setup.main(["--profile", str(file), "doctor", "--key", "cli-doctor"]) == 2
    output = capsys.readouterr().out
    assert token not in output and "setup_prerequisite_or_server_denied" in output


def test_profile_forbids_caller_authorization_flags(configured):
    _, _, profile = configured
    with pytest.raises(ValidationError):
        SetupProfile.model_validate({**profile, "installed": True})


@pytest.mark.parametrize("fault", ["expired", "max-age"])
def test_original_financial_times_denied(configured, tmp_path, fault):
    data, file, profile = configured
    observed = datetime.now(UTC) - timedelta(days=1)
    payload = {
        **data.financial_body,
        "observed_at": observed.isoformat(),
        "valid_until": (observed + timedelta(hours=1)).isoformat()
        if fault == "expired"
        else (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    }
    source = tmp_path / "original-observation.json"
    private_json(source, payload)
    try:
        result = invoke(file, "operator-financial-ref", input=str(source), key="old-observation")
    except setup.SetupDenied:
        pass  # Issuance may itself enforce current bounds.
    else:
        assert result["observed_at"] == payload["observed_at"]
        profile["resolve_request"]["financial_ref"] = result["attestation_ref"]
        private_json(file, profile)
        with pytest.raises(setup.SetupDenied):
            invoke(file, "doctor", key="old-doctor")
    assert not data.marker.exists()


def test_python_module_from_new_user_directory(configured, tmp_path):
    _, file, _ = configured
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tessera_mcp.runtime_setup",
            "--profile",
            str(file),
            "doctor",
            "--key",
            "cold-module-doctor",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["provider_billing_guarantee"] == "unknown"


def test_reconcile_is_actual_operation_readback_only(configured):
    data, file, _ = configured
    operation = data.client.allocate(data.allocation, "setup-readback-allocation")
    result = invoke(file, "reconcile", ref=operation.operation_ref)
    assert result["state"] == "allocated"
    assert result["status"] == "readback-only-not-continuation"
    assert invoke(file, "reconcile", ref=operation.operation_ref)["state"] == "allocated"
    assert not data.marker.exists()


def test_private_owner_exception_is_scrubbed(configured, capsys):
    _, file, profile = configured
    adapter = Path(profile["owner"]["adapter_file"])
    adapter.write_text(
        "def open_installed_owner(binding, **kw):\n"
        "    raise RuntimeError('PRIVATE_OWNER_TEST_SECRET')\n"
    )
    profile["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
    private_json(file, profile)
    assert setup.main(["--profile", str(file), "doctor", "--key", "owner-error"]) == 2
    assert "PRIVATE_OWNER_TEST_SECRET" not in capsys.readouterr().out


def test_foreign_subject_scope_is_denied(configured):
    _, file, profile = configured
    profile["enrollment_request"]["subject_credential_id"] = "foreign-subject"
    private_json(file, profile)
    with pytest.raises(setup.SetupDenied, match="installed_owner_scope"):
        invoke(file, "doctor", key="foreign-subject-doctor")


def test_actual_worker_publisher_audience(configured, tmp_path):
    from tessera_control.store import Store

    data, file, profile = configured
    # Existing temporary authenticated worker fixture, not a production secret.
    worker = Store(data.state).authenticate_worker("worker-token")
    request = profile["enrollment_request"]
    old_id = request["publisher_credential_id"]
    request["publisher_credential_id"] = worker["credential_id"]
    request["publisher_audience"] = "worker"
    profile["audience"] = "worker"
    Path(profile["publisher_credential_file"]).write_text("worker-token")
    adapter = Path(profile["owner"]["adapter_file"])
    adapter.write_text(adapter.read_text().replace(repr(old_id), repr(worker["credential_id"])))
    profile["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
    private_json(file, profile)
    enrollment = invoke(file, "enroll", key="worker-setup-enroll")
    finance = {**data.financial_body, "enrollment_ref": enrollment["enrollment_ref"]}
    input_file = tmp_path / "worker-financial.json"
    private_json(input_file, finance)
    issued = invoke(
        file, "operator-financial-ref", input=str(input_file), key="worker-setup-finance"
    )
    profile["resolve_request"]["enrollment_ref"] = enrollment["enrollment_ref"]
    profile["resolve_request"]["financial_ref"] = issued["attestation_ref"]
    private_json(file, profile)
    assert (
        invoke(file, "doctor", key="worker-setup-doctor")["scope"]["enrollment"]
        == enrollment["enrollment_ref"]
    )
    profile["audience"] = "control"
    profile["enrollment_request"]["publisher_audience"] = "control"
    private_json(file, profile)
    with pytest.raises(setup.SetupDenied):
        invoke(file, "doctor", key="wrong-worker-audience")
    assert not data.marker.exists()
