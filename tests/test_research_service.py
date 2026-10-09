"""Source factory/CLI failclosed checks; no credential or harness operations."""

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from urllib.request import urlopen

import pytest
import test_runtime_admission as admission_fixtures
import test_runtime_admission_integration as integration_fixtures

from tessera_agent import research_service
from tessera_agent.research_consumer_store import ConsumerBlocked
from tessera_agent.research_factory import ProductionResearchFactory
from tessera_agent.research_journey import ResearchJourney
from tessera_control import cli
from tessera_control.runtime_bindings import NativeBridgeUnavailable
from tessera_control.runtime_composition import (
    RuntimeOwnerComposition,
    RuntimeOwnerConfig,
    load_runtime_owner,
)
from tessera_mcp.harness_binding import HarnessBinding
from tessera_mcp.harness_observer import EvidenceBackedHarnessObserver
from tessera_mcp.runtime_admission import AdmissionDenied

synthetic_v2 = admission_fixtures.synthetic_v2
consumer_setup = integration_fixtures.consumer_setup
output_setup = integration_fixtures.output_setup
arguments = integration_fixtures.arguments


def test_default_cli_validates_only_explicit_config_before_admission(monkeypatch, capsys):
    reads = []

    def config_only(path):
        reads.append(str(path))
        return b'{"schema_version":"unsupported"}'

    monkeypatch.setattr(research_service, "private_file", config_only)
    assert (
        research_service.main(
            ["--config", "/private/unread/profile", "--state-dir", "/private/unread/state"]
        )
        == 78
    )
    assert "startup blocked" in capsys.readouterr().err
    assert reads == ["/private/unread/profile"]


def test_production_factory_is_normal_wiring_not_unconditional_exit78():
    from tessera_agent.research_factory import ProductionResearchFactory

    assert isinstance(research_service.production_factory(), ProductionResearchFactory)


def test_no_api_key_or_fixture_boolean_admission_config():
    with pytest.raises(ValueError):
        research_service.ResearchReferences.model_validate(
            {"account_mode": "api-key", "admitted": True}
        )


def test_reference_rejects_remote_plaintext_and_relative_credentials():
    with pytest.raises(ValueError):
        research_service.RoleReferences.origin("http://remote.example")
    with pytest.raises(ValueError):
        research_service.RoleReferences.reference("relative/credential")


def private(path, data):
    path.write_text(data)
    path.chmod(0o600)
    return path


@pytest.fixture
def owner_files(tmp_path):
    """SYNTHETIC owner signature/process/resources; actual file loader only.

    This deliberately uses fixture readbacks, NOT an accepted production owner.
    No OAuth, native resource lock, provider or real signed publisher is present.
    """
    resources = private(tmp_path / "resources.json", '{"current":true}')
    manifest = private(tmp_path / "manifest.json", '{"SYNTHETIC":"manifest"}')
    events = tmp_path / "events.txt"
    adapter = private(
        tmp_path / "owner.py",
        f"""
import json
from pathlib import Path
from tessera_control.runtime_composition import RuntimeOwnerRegistration
from tessera_mcp.runtime_setup_models import InstalledOwnerCapability
events = Path({str(events)!r})
def record(value):
    with events.open('a') as stream: stream.write(value+'\\n')
record('loaded')
def open_runtime_owner(config, *, state_dir):
    record('opened')
    def current():
        state = json.loads(Path(config.owner.resource_reference_file).read_text())
        if not state.get('current'): raise Exception('SYNTHETIC-PRIVATE-FAILURE')
        if state.get('boolean'): return True
    class Adapter:
        def verify_enrollment(self, *args): record('enrollment'); current()
        def verify_allocation(self, *args): record('allocation'); current()
        def verify_claim(self, *args): record('claim'); return {{'admitted': True}}
    def observer(*args): return {{'complete': True}}
    return RuntimeOwnerRegistration(
        InstalledOwnerCapability(config.expected_readback, current),
        Adapter(), observer, lambda: record('closed'))
""",
    )

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    readback = {
        "manifest_sha256": digest(manifest),
        "signature_reference": "SYNTHETIC-signature",
        "approval_reference": "SYNTHETIC-operator-approval",
        "process_link_reference": "SYNTHETIC-process-link",
        "publisher_ref": "a" * 64,
        "opaque_owner_ref": "b" * 64,
        "session_ref": "c" * 64,
        "subject_credential_id": "SYNTHETIC-subject",
        "publisher_credential_id": "SYNTHETIC-publisher",
        "pins": {
            "instance": "SYNTHETIC-instance",
            **{
                name: "d" * 64
                for name in (
                    "build",
                    "sdk",
                    "harness_schema",
                    "effective_config",
                    "loaded_skill",
                    "typed_catalog",
                    "tool_mapping",
                    "binding",
                    "terminal_implementation",
                )
            },
        },
    }
    config = {
        "version": "tessera-runtime-owner-composition-v1",
        "owner": {
            "adapter_file": str(adapter),
            "adapter_sha256": digest(adapter),
            "resource_reference_file": str(resources),
            "signed_manifest_file": str(manifest),
            "approved_manifest_sha256": digest(manifest),
            "approval_reference": readback["approval_reference"],
        },
        "expected_readback": readback,
    }
    file = private(tmp_path / "owner-config.json", json.dumps(config))
    return file, digest(file), config, events, resources


def test_ordinary_serve_default_keeps_runtime_unregistered(tmp_path, monkeypatch):
    from tessera_control.store import initialize_state

    state = tmp_path / "state"
    initialize_state(state)
    apps = []
    monkeypatch.setattr(
        sys, "argv", ["tessera", "serve", "--state-dir", str(state), "--port", "8123"]
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: apps.append(app))
    assert cli.main() == 0
    assert apps[0].state.runtime_bindings.adapter is None


def test_ordinary_serve_approved_configuration_reaches_real_app_store(
    owner_files, tmp_path, monkeypatch
):
    from tessera_control.store import initialize_state

    file, digest, _, events, _ = owner_files
    state = tmp_path / "state"
    initialize_state(state)
    apps = []

    def run(app, **kwargs):
        apps.append(app)
        app.state.runtime_bindings.adapter.verify_enrollment(None, None, None, None, None)

    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "serve",
            "--state-dir",
            str(state),
            "--port",
            "8123",
            "--runtime-owner-config",
            str(file),
            "--runtime-owner-sha256",
            digest,
        ],
    )
    assert cli.main() == 0
    assert events.read_text().splitlines() == ["loaded", "opened", "enrollment", "closed"]
    with pytest.raises(NativeBridgeUnavailable, match="closed"):
        apps[0].state.runtime_bindings.adapter.verify_allocation(None, None, None, None)


@pytest.mark.parametrize(
    "fault",
    [
        "approval",
        "unknown",
        "duplicate",
        "adapter",
        "manifest",
        "symlink",
        "public_config",
        "public_adapter",
        "boolean",
        "expired",
        "readback",
        "unsupported",
        "secret_exception",
    ],
)
def test_owner_config_fails_closed_without_secret_output(owner_files, tmp_path, fault, capsys):
    file, digest, config, events, resources = owner_files
    adapter = tmp_path / "owner.py"
    if fault == "approval":
        digest = "0" * 64
    elif fault == "unknown":
        config["admitted"] = True
    elif fault == "duplicate":
        file.write_text(
            file.read_text().replace('{"version":', '{"version":"unsupported","version":', 1)
        )
    elif fault == "adapter":
        adapter.write_text("raise Exception('SYNTHETIC-PRIVATE-FAILURE')")
    elif fault == "manifest":
        (tmp_path / "manifest.json").write_text("changed")
    elif fault == "symlink":
        link = tmp_path / "link.py"
        link.symlink_to(adapter)
        config["owner"]["adapter_file"] = str(link)
    elif fault == "public_config":
        file.chmod(0o644)
    elif fault == "public_adapter":
        adapter.chmod(0o644)
    elif fault == "boolean":
        resources.write_text('{"current":true,"boolean":true}')
    elif fault == "expired":
        resources.write_text('{"current":false}')
    elif fault == "readback":
        config["expected_readback"]["approval_reference"] = "wrong-approval"
    elif fault == "unsupported":
        adapter.write_text("def open_runtime_owner(*args, **kwargs): return {'complete':True}")
        config["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
    elif fault == "secret_exception":
        adapter.write_text("raise Exception('SYNTHETIC-PRIVATE-FAILURE')")
        config["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
    if fault not in {"approval", "duplicate", "public_config"}:
        file.write_text(json.dumps(config))
    if fault != "approval":
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
    with pytest.raises(NativeBridgeUnavailable) as denied:
        load_runtime_owner(file, digest, state_dir=tmp_path / "state")
    assert str(denied.value) == "approved_runtime_owner_configuration_required"
    assert capsys.readouterr() == ("", "")
    if fault in {
        "approval",
        "unknown",
        "duplicate",
        "adapter",
        "manifest",
        "symlink",
        "public_config",
        "public_adapter",
        "readback",
    }:
        assert not events.exists()


def test_current_owner_rechecked_before_each_method_and_json_claim_never_authority(
    owner_files, tmp_path
):
    file, digest, _, events, resources = owner_files
    owner = load_runtime_owner(file, digest, state_dir=tmp_path / "state")
    adapter = owner.control_adapter()
    adapter.verify_allocation(None, None, None, None)
    with pytest.raises(NativeBridgeUnavailable, match="native_owner_check_window"):
        adapter.verify_claim(None, None, None, None)
    resources.write_text('{"current":false}')
    with pytest.raises(NativeBridgeUnavailable, match="current_linkage"):
        adapter.verify_allocation(None, None, None, None)
    assert events.read_text().splitlines().count("allocation") == 1
    owner.close()
    owner.close()
    assert events.read_text().splitlines().count("closed") == 1


def test_explicit_production_research_factory_loads_owner_no_default_conversion(
    owner_files, tmp_path
):
    file, digest = research_owner_files(owner_files, tmp_path)
    _, _, config, events, _ = owner_files
    factory = research_service.production_factory(
        owner_config=file, owner_sha256=digest, state_dir=tmp_path / "state"
    )
    assert callable(factory.admission_observer)
    with pytest.raises(NativeBridgeUnavailable, match="enrolled_runtime_admission_observer"):
        factory.admission_observer(
            SimpleNamespace(credential_id=config["expected_readback"]["subject_credential_id"]),
            None,
            {},
            [],
        )
    factory.close()
    assert events.read_text().splitlines()[-1] == "closed"
    default = research_service.production_factory()
    assert default.admission_observer is None
    default.close()


def test_supported_v2_observer_uses_actual_verifier_and_rejects_current_owner_change(
    owner_files, tmp_path, synthetic_v2
):
    file, digest, config, _, resources = owner_files
    loaded = load_runtime_owner(file, digest, state_dir=tmp_path / "state")
    binding = HarnessBinding(b"{}", b"{}", b"fixture-skill")
    verifier = synthetic_v2.make()
    verifier.pins = verifier.pins.model_copy(update={"binding": binding.digest})
    preparations = []

    def prepare():
        preparations.append("prepared")
        return admission_fixtures.encoded(synthetic_v2), synthetic_v2.expected

    actual = EvidenceBackedHarnessObserver(
        binding=binding,
        verifier=verifier,
        read_preparation=prepare,
    )
    config["expected_readback"].update(
        pins=verifier.pins.model_dump(),
        publisher_ref=verifier.scope.publisher_ref,
        opaque_owner_ref=verifier.scope.opaque_owner_ref,
        session_ref=verifier.scope.session_ref,
        subject_credential_id=verifier.scope.credential,
    )
    measured = RuntimeOwnerConfig.model_validate(config).expected_readback
    registration = replace(
        loaded.registration,
        admission_observer=lambda *args: actual,
        capability=replace(loaded.registration.capability, readback=measured),
    )
    owner = RuntimeOwnerComposition(RuntimeOwnerConfig.model_validate(config), registration)
    ref = SimpleNamespace(
        credential_id=verifier.scope.credential, observer_admission_id=verifier.scope.enrollment
    )
    observed = owner.admission_observer(ref, binding, {}, [])
    assert observed.verifier is verifier
    altered = HarnessBinding(binding.config, b'{"changed":"manifest-only"}', binding.skill)
    assert altered.digest != binding.digest
    with pytest.raises(NativeBridgeUnavailable, match="runtime_observer_owner_binding_changed"):
        owner.admission_observer(ref, altered, {}, [])
    assert preparations == []
    assert observed.read_preparation() == (
        admission_fixtures.encoded(synthetic_v2),
        synthetic_v2.expected,
    )
    # Actual verifier consumes a one-shot ledger entry; replay remains rejected.
    replay_verifier = synthetic_v2.make()
    replay_verifier.verify(
        admission_fixtures.encoded(synthetic_v2),
        operation="session",
        expected=synthetic_v2.expected,
    )
    with pytest.raises(AdmissionDenied):
        replay_verifier.verify(
            admission_fixtures.encoded(synthetic_v2),
            operation="session",
            expected=synthetic_v2.expected,
        )
    resources.write_text('{"current":false}')
    with pytest.raises(NativeBridgeUnavailable, match="current_linkage"):
        owner.admission_observer(ref, binding, {}, [])
    with pytest.raises(NativeBridgeUnavailable, match="current_linkage"):
        observed.read_preparation()
    owner.close()


@pytest.mark.parametrize("fault", ["boolean", "exception", "expired_after", "wrong_pins"])
def test_registered_callbacks_never_admit_from_boolean_error_or_changed_owner(
    owner_files, tmp_path, fault
):
    file, digest, config, _, resources = owner_files
    loaded = load_runtime_owner(file, digest, state_dir=tmp_path / "state")

    def verify(*args):
        if fault == "exception":
            raise Exception("SYNTHETIC-PRIVATE-FAILURE")
        if fault == "expired_after":
            resources.write_text('{"current":false}')
            return None
        return True

    registration = replace(
        loaded.registration,
        control_adapter=SimpleNamespace(
            verify_enrollment=verify,
            verify_allocation=verify,
            verify_claim=verify,
        ),
    )
    owner = RuntimeOwnerComposition(RuntimeOwnerConfig.model_validate(config), registration)
    adapter = owner.control_adapter()
    if fault == "wrong_pins":
        changed = registration.capability.readback.model_copy(update={"publisher_ref": "0" * 64})
        owner.registration = replace(
            registration, capability=replace(registration.capability, readback=changed)
        )
    with pytest.raises(NativeBridgeUnavailable) as denied:
        adapter.verify_allocation(None, None, None, None)
    assert "SYNTHETIC-PRIVATE" not in str(denied.value)
    owner.close()


def test_owner_adapter_attribute_error_is_sanitized(owner_files, tmp_path):
    file, digest, config, _, _ = owner_files
    loaded = load_runtime_owner(file, digest, state_dir=tmp_path / "state")

    class BrokenAdapter:
        @property
        def verify_enrollment(self):
            raise ValueError("SYNTHETIC-PRIVATE-FAILURE")

    registration = replace(loaded.registration, control_adapter=BrokenAdapter())
    owner = RuntimeOwnerComposition(RuntimeOwnerConfig.model_validate(config), registration)
    with pytest.raises(NativeBridgeUnavailable, match="runtime_control_adapter_unavailable"):
        owner.control_adapter()
    owner.close()


@pytest.mark.parametrize(
    "flags",
    [
        ["--runtime-owner-config", "/not/approved"],
        ["--runtime-owner-sha256", "0" * 64],
    ],
)
def test_ordinary_serve_incomplete_owner_config_never_starts_server(
    flags, tmp_path, monkeypatch, capsys
):
    started = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: started.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        ["tessera", "serve", "--state-dir", str(tmp_path / "state"), "--port", "8123", *flags],
    )
    assert cli.main() == 78
    assert not started
    assert capsys.readouterr().err == "serve runtime owner configuration blocked\n"
    assert not (tmp_path / "state").exists()


def research_owner_files(owner_files, tmp_path):
    file, digest, config, _, _ = owner_files
    other = json.loads(json.dumps(config))
    other["expected_readback"].update(
        subject_credential_id="SYNTHETIC-critic-subject",
        publisher_credential_id="SYNTHETIC-critic-publisher",
        publisher_ref="e" * 64,
        opaque_owner_ref="f" * 64,
        session_ref="1" * 64,
    )
    resources = private(tmp_path / "critic-resources.json", '{"current":true}')
    other["owner"]["resource_reference_file"] = str(resources)
    critic = private(tmp_path / "critic-owner.json", json.dumps(other))
    root = {
        "version": "tessera-research-owner-composition-v1",
        "planner": {"config_file": str(file), "approved_sha256": digest},
        "critic": {
            "config_file": str(critic),
            "approved_sha256": hashlib.sha256(critic.read_bytes()).hexdigest(),
        },
    }
    root_file = private(tmp_path / "research-owner.json", json.dumps(root))
    return root_file, hashlib.sha256(root_file.read_bytes()).hexdigest()


def research_references(tmp_path):
    roles = {}
    for role in ("planner", "critic"):
        roles[role] = {
            "admission_version": "v2",
            "admission_policy_ref": "approved-policy",
            "admission_policy_digest": "a" * 64,
            "model": "openai/model",
            "principal_id": role,
            "credential_id": role + "-credential",
            "run_id": role + "-run",
            "context_id": role + "-context",
            "policy_id": role + "-policy",
            "observer_admission_id": role + "-enrollment",
            "registration_grant_id": role + "-grant",
            "registration_revision": 1,
            "harness_origin": f"https://{role}.example",
            "peer_origin": f"https://peer-{role}.example",
            **{
                name: str(tmp_path / (role + "-" + name))
                for name in (
                    "agent_token_file",
                    "harness_password_file",
                    "binding_manifest_file",
                    "binding_config_file",
                    "skill_file",
                    "capture_binding_file",
                    "reasoning_profile_file",
                    "peer_token_file",
                )
            },
        }
    return {
        "schema_version": "tessera-research-service-references-v1",
        "journey_id": "journey",
        "project_id": "project",
        "account_mode": "included-chatgpt-oauth",
        "control_origin": "https://control.example",
        "control_ca_file": str(tmp_path / "ca"),
        "coding_origins": ["https://coding.example"],
        **roles,
    }


def test_ordinary_research_cli_explicit_v2_loads_and_closes_independent_owners(
    owner_files, tmp_path, capsys
):
    owner_file, digest = research_owner_files(owner_files, tmp_path)
    config = private(tmp_path / "research-config.json", json.dumps(research_references(tmp_path)))
    _, _, _, events, _ = owner_files
    args = ["--config", str(config), "--state-dir", str(tmp_path / "state")]
    # Without explicit owner wiring v2 remains closed, before ANY role file read.
    assert research_service.main(args) == 78
    assert not events.exists()
    # Actual ordinary CLI config loads approved executable bytes, then still
    # refuses missing installed role bindings; never starts session/inference.
    assert (
        research_service.main(
            args + ["--runtime-owner-config", str(owner_file), "--runtime-owner-sha256", digest]
        )
        == 78
    )
    assert events.read_text().splitlines() == [
        "loaded",
        "opened",
        "loaded",
        "opened",
        "closed",
        "closed",
    ]
    assert "SYNTHETIC-PRIVATE" not in capsys.readouterr().err
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "fault", ["shared_subject", "shared_resource", "changed_role_digest", "unknown_root"]
)
def test_research_independent_owner_configuration_fails_closed(owner_files, tmp_path, fault):
    file, digest = research_owner_files(owner_files, tmp_path)
    config = json.loads(file.read_text())
    critic_file = tmp_path / "critic-owner.json"
    critic = json.loads(critic_file.read_text())
    if fault == "shared_subject":
        critic["expected_readback"]["subject_credential_id"] = owner_files[2]["expected_readback"][
            "subject_credential_id"
        ]
    elif fault == "shared_resource":
        critic["owner"]["resource_reference_file"] = str(owner_files[4])
    elif fault == "changed_role_digest":
        config["critic"]["approved_sha256"] = "0" * 64
    elif fault == "unknown_root":
        config["authenticated"] = True
    if fault in {"shared_subject", "shared_resource"}:
        critic_file.write_text(json.dumps(critic))
        config["critic"]["approved_sha256"] = hashlib.sha256(critic_file.read_bytes()).hexdigest()
    file.write_text(json.dumps(config))
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    with pytest.raises(NativeBridgeUnavailable, match="approved_research_owner_configuration"):
        research_service.production_factory(
            owner_config=file, owner_sha256=digest, state_dir=tmp_path / "state"
        )


@pytest.mark.parametrize("consumer_setup", ["planner"], indirect=True)
def test_cached_enrolled_observer_cannot_admit_changed_manifest(consumer_setup, synthetic_v2):
    constructor, calls, *_ = consumer_setup
    observer = integration_fixtures.align(synthetic_v2, constructor)
    prepared = []
    observer.read_preparation = lambda: prepared.append("must-not-prepare")
    binding = constructor["harness_binding"]
    manifest = json.loads(binding.manifest)
    manifest["SYNTHETIC-manifest-change"] = True
    changed = HarnessBinding(binding.config, json.dumps(manifest).encode(), binding.skill)
    assert changed.config == binding.config and changed.skill == binding.skill
    assert changed.digest != binding.digest
    ref = SimpleNamespace(
        admission_version="v2",
        guard=None,
        admission_policy_ref=synthetic_v2.policy.admission_policy_ref,
        admission_policy_digest=synthetic_v2.policy.digest,
    )
    config = SimpleNamespace(
        control_origin="https://control.example",
        coding_origins=[],
        planner=SimpleNamespace(**vars(ref), harness_origin="https://planner.example"),
        critic=SimpleNamespace(**vars(ref), harness_origin="https://critic.example"),
    )
    factory = ProductionResearchFactory(admission_observer=lambda *args: observer)
    with pytest.raises(ConsumerBlocked, match="approved_v2_factory_policy_or_observer_missing"):
        factory.attested_observer(config, ref, changed, constructor["capture_binding"], [])
    assert calls == [] and prepared == []  # No preparation/MCP/session effects.


@pytest.mark.parametrize("resource_list", [True, False])
def test_journey_closes_remaining_resources_after_private_error(resource_list):
    events = []

    def resource(name, fails=False):
        def close():
            events.append(name)
            if fails:
                raise ValueError("SYNTHETIC-PRIVATE-TEARDOWN")

        return SimpleNamespace(close=close)

    first, second, third = resource("first", True), resource("second"), resource("third")
    journey = object.__new__(ResearchJourney)
    if resource_list:
        journey.resources = [third, second, first]
    else:
        journey.planner = SimpleNamespace(peer=first, collector=second, store=third)
        journey.critic = SimpleNamespace(peer=first, collector=None, store=third)
    with pytest.raises(ConsumerBlocked) as denied:
        journey.close()
    assert str(denied.value) == "research_journey_shutdown_failed"
    assert events == ["first", "second", "third"]
    if resource_list:
        assert journey.resources == []
        journey.close()
        assert events == ["first", "second", "third"]


@pytest.mark.parametrize("primary", ["completed", "canceled", "exception"])
@pytest.mark.parametrize("owner_fails", [False, True])
@pytest.mark.parametrize("resource_fails", [False, True])
def test_cli_teardown_always_closes_owner_and_preserves_primary_exit(
    tmp_path, monkeypatch, capsys, primary, owner_fails, resource_fails
):
    events = []

    def close_resource(name):
        events.append(name)
        if name == "first" and resource_fails:
            raise ValueError("SYNTHETIC-PRIVATE-TEARDOWN")

    journey = object.__new__(ResearchJourney)
    journey.resources = [
        SimpleNamespace(close=lambda: close_resource("last")),
        SimpleNamespace(close=lambda: close_resource("first")),
    ]

    def advance():
        if primary == "exception":
            raise ValueError("SYNTHETIC-PRIVATE-PRIMARY")
        return {"stage": primary}

    journey.advance = advance

    def owner_close():
        events.append("owner")
        if owner_fails:
            raise ValueError("SYNTHETIC-PRIVATE-OWNER")

    factory = SimpleNamespace(close=owner_close)
    monkeypatch.setattr(research_service, "private_file", lambda path: b"{}")
    monkeypatch.setattr(research_service, "production_factory", lambda **kwargs: factory)
    monkeypatch.setattr(research_service, "create_journey", lambda *args: journey)
    code = research_service.main(
        ["--config", str(tmp_path / "references"), "--state-dir", str(tmp_path / "state")]
    )
    expected = 2 if primary == "canceled" else 78
    if primary == "completed" and not (owner_fails or resource_fails):
        expected = 0
    assert code == expected
    assert events == ["first", "last", "owner"]
    output = capsys.readouterr()
    assert "SYNTHETIC-PRIVATE" not in output.out + output.err
    assert ("Tessera research shutdown blocked." in output.err) == (owner_fails or resource_fails)
    if primary == "exception":
        assert "startup blocked" in output.err


def test_exact_owner_file_fifo_rejected_in_bounded_subprocess(tmp_path):
    fifo = tmp_path / "owner.fifo"
    os.mkfifo(fifo, 0o600)
    script = """
import sys
from tessera_mcp.runtime_setup import exact_file, SetupDenied
try:
    exact_file(sys.argv[1], private=True)
except SetupDenied as error:
    assert str(error) == 'bounded_regular_file_required'
else:
    raise AssertionError('FIFO accepted')
"""
    run = subprocess.run(
        [sys.executable, "-B", "-c", script, str(fifo)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == run.stderr == ""


@pytest.mark.parametrize("failure", [None, "startup", "shutdown", "construction"])
@pytest.mark.parametrize("owner_fails", [False, True])
def test_serve_retains_existing_lifespan_and_closes_owner_once(
    owner_files, tmp_path, monkeypatch, capsys, failure, owner_fails
):
    import asyncio

    from fastapi import FastAPI

    file, digest, config, events, _ = owner_files
    if owner_fails:
        adapter = Path(config["owner"]["adapter_file"])
        code = adapter.read_text().replace("lambda: record('closed')", "close")
        code += (
            "\ndef close():\n    record('closed')\n"
            "    raise ValueError('SYNTHETIC-PRIVATE-CLOSE')\n"
        )
        adapter.write_text(code)
        config["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
        file.write_text(json.dumps(config))
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
    lifecycle = []

    @asynccontextmanager
    async def previous(app):
        lifecycle.append("startup")
        if failure == "startup":
            raise RuntimeError("synthetic startup failure")
        try:
            yield {"existing_state": 42}
        finally:
            lifecycle.append("shutdown")
            if failure == "shutdown":
                raise RuntimeError("synthetic shutdown failure")

    def make_app(*args, **kwargs):
        if failure == "construction":
            raise RuntimeError("synthetic construction failure")
        return FastAPI(lifespan=previous)

    def run(app, **kwargs):
        async def cycle():
            async with app.router.lifespan_context(app) as state:
                assert state == {"existing_state": 42}

        asyncio.run(cycle())

    monkeypatch.setattr(cli, "create_app", make_app)
    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "serve",
            "--state-dir",
            str(tmp_path / "state"),
            "--port",
            "8123",
            "--runtime-owner-config",
            str(file),
            "--runtime-owner-sha256",
            digest,
        ],
    )
    if failure:
        with pytest.raises(RuntimeError, match=f"synthetic {failure} failure"):
            cli.main()
    elif owner_fails:
        with pytest.raises(RuntimeError, match="runtime owner shutdown failed"):
            cli.main()
    else:
        assert cli.main() == 0
    assert events.read_text().splitlines() == ["loaded", "opened", "closed"]
    if failure != "construction":
        assert lifecycle[0] == "startup"
    output = capsys.readouterr()
    assert "SYNTHETIC-PRIVATE-CLOSE" not in output.out + output.err
    if owner_fails:
        assert "serve runtime owner shutdown blocked" in output.err


@pytest.mark.parametrize("mode", ["default", "configured", "close_error"])
def test_actual_ordinary_cli_sigterm_closes_owner_before_signal_reraise(
    owner_files, tmp_path, monkeypatch, mode
):
    """Real source CLI/Uvicorn/HTTP/SIGTERM; owner facts explicitly SYNTHETIC."""
    from tessera_control.store import initialize_state

    file, digest, config, events, _ = owner_files
    if mode == "close_error":
        adapter = Path(config["owner"]["adapter_file"])
        code = adapter.read_text().replace("lambda: record('closed')", "close")
        code += (
            "\ndef close():\n    record('closed')\n"
            "    raise ValueError('SYNTHETIC-PRIVATE-CLOSE')\n"
        )
        adapter.write_text(code)
        config["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
        file.write_text(json.dumps(config))
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
    state = tmp_path / "state"
    initialize_state(state)
    persistent_root = Path.cwd() / ".tessera/receipts/rc-v10-runtime"
    clean = tmp_path / "clean-working-directory"
    clean.mkdir()
    assert not (clean / ".tessera").exists()
    monkeypatch.chdir(clean)
    # No external executables (including lsof) are available to this probe.
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    monkeypatch.setenv("PATH", str(empty_bin))
    receipt = (
        Path.cwd()
        / ".tessera/receipts/rc-v10-runtime"
        / ("sigterm-" + mode + "-" + uuid.uuid4().hex[:12])
    )
    receipt.mkdir(mode=0o700, parents=True)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]

    source = Path(cli.__file__).resolve()
    shim = (
        "from pathlib import Path; from tessera_control import cli; "
        f"assert Path(cli.__file__).resolve() == Path({str(source)!r}); "
        "raise SystemExit(cli.main())"
    )
    command = [
        sys.executable,
        "-B",
        "-c",
        shim,
        "serve",
        "--state-dir",
        str(state),
        "--port",
        str(port),
    ]
    if mode != "default":
        command += ["--runtime-owner-config", str(file), "--runtime-owner-sha256", digest]
    process = None
    healthy = False
    owned_bind = False
    sent_sigterm = False
    with (
        (receipt / "stdout.log").open("wb") as stdout,
        (receipt / "stderr.log").open("wb") as stderr,
    ):
        try:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline and process.poll() is None:
                log = (receipt / "stderr.log").read_text()
                # These are this child's captured Uvicorn logs, emitted only
                # after its successful bind. A port race fails, never kills a
                # different listener or accepts unrelated HTTP readiness alone.
                owned_bind = (
                    f"Started server process [{process.pid}]" in log
                    and f"Uvicorn running on http://127.0.0.1:{port}" in log
                    and "Application startup complete" in log
                    and process.poll() is None
                )
                if owned_bind:
                    with urlopen(f"http://127.0.0.1:{port}/health/ready", timeout=1) as response:
                        healthy = response.status == 200
                    break
                time.sleep(0.05)
            assert healthy and owned_bind and process.poll() is None
            process.send_signal(signal.SIGTERM)
            sent_sigterm = True
            process.wait(timeout=15)
        finally:
            if process is not None and process.poll() is None:
                # Bound cleanup only of this exact child, including failed test runs.
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            event_bytes = events.read_bytes() if events.exists() else b""
            (receipt / "events.txt").write_bytes(event_bytes)
            (receipt / "result.json").write_text(
                json.dumps(
                    {
                        "argv": command,
                        "mode": mode,
                        "pid": process.pid if process else None,
                        "native_exit": process.returncode if process else None,
                        "port": port,
                        "healthy": healthy,
                        "owned_bind_from_child_uvicorn": owned_bind,
                        "clean_working_directory": str(clean),
                        "external_executable_path_empty": True,
                        "own_SIGTERM": sent_sigterm,
                        "cli_source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "upstream": (
                            "TESTONLY SYNTHETIC owner; actual source CLI/Uvicorn/HTTP/signal; "
                            "not installed candidate"
                        ),
                    },
                    sort_keys=True,
                    indent=2,
                )
            )
            stdout.flush()
            stderr.flush()
            persistent_root.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copytree(receipt, persistent_root / receipt.name)
    assert process.returncode == -signal.SIGTERM
    with socket.socket() as probe:
        probe.settimeout(1)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
    output = (receipt / "stdout.log").read_text() + (receipt / "stderr.log").read_text()
    assert "SYNTHETIC-PRIVATE-CLOSE" not in output
    if mode == "default":
        assert not event_bytes
    else:
        assert event_bytes.decode().splitlines() == ["loaded", "opened", "closed"]
    if mode == "close_error":
        assert "serve runtime owner shutdown blocked" in output
        assert "Application shutdown failed" in output
    else:
        assert "Application shutdown complete" in output


@pytest.mark.parametrize("runner_fails", [False, True])
def test_serve_fallback_cleanup_failure_does_not_silence_primary_error(
    owner_files, tmp_path, monkeypatch, capsys, runner_fails
):
    from tessera_control.store import initialize_state

    file, _, config, events, _ = owner_files
    adapter = Path(config["owner"]["adapter_file"])
    code = adapter.read_text().replace("lambda: record('closed')", "close")
    code += (
        "\ndef close():\n    record('closed')\n    raise ValueError('SYNTHETIC-PRIVATE-CLOSE')\n"
    )
    adapter.write_text(code)
    config["owner"]["adapter_sha256"] = hashlib.sha256(adapter.read_bytes()).hexdigest()
    file.write_text(json.dumps(config))
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    state = tmp_path / "state"
    initialize_state(state)

    def run(*args, **kwargs):
        if runner_fails:
            raise RuntimeError("synthetic runner failure")

    monkeypatch.setattr(cli.uvicorn, "run", run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tessera",
            "serve",
            "--state-dir",
            str(state),
            "--port",
            "8123",
            "--runtime-owner-config",
            str(file),
            "--runtime-owner-sha256",
            digest,
        ],
    )
    if runner_fails:
        with pytest.raises(RuntimeError, match="synthetic runner failure"):
            cli.main()
    else:
        assert cli.main() == 78
    assert events.read_text().splitlines() == ["loaded", "opened", "closed"]
    output = capsys.readouterr()
    assert output.err == "serve runtime owner shutdown blocked\n"
    assert "SYNTHETIC-PRIVATE-CLOSE" not in output.out + output.err
