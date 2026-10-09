"""Explicit operator-approved owner composition; configuration is not authority.

The owner adapter is trusted executable code. Its config digest must be approved
outside agent-controlled inputs. No module names, environment discovery, default
plugins or serialized admission verdicts are supported.
"""

import hashlib
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from tessera_mcp.runtime_admission import closed_object
from tessera_mcp.runtime_setup import exact_file
from tessera_mcp.runtime_setup_models import (
    InstalledOwnerCapability,
    InstalledReadback,
    OwnerBinding,
)

from .runtime_binding_models import Digest, RuntimeBindingDTO
from .runtime_bindings import NativeBridgeUnavailable, NativeCheckWindow


class RuntimeOwnerConfig(RuntimeBindingDTO):
    version: Literal["tessera-runtime-owner-composition-v1"]
    owner: OwnerBinding
    expected_readback: InstalledReadback


class RuntimeOwnerReference(RuntimeBindingDTO):
    config_file: str
    approved_sha256: Digest


class ResearchOwnerConfig(RuntimeBindingDTO):
    version: Literal["tessera-research-owner-composition-v1"]
    planner: RuntimeOwnerReference
    critic: RuntimeOwnerReference


@dataclass(frozen=True)
class RuntimeOwnerRegistration:
    """Trusted owner's synchronous interfaces, never constructible from JSON.

    The capability rechecks genuine signed-manifest, loaded-process and private
    resource linkage. Adapter methods enforce the existing Store contract; the
    observer must be backed by the existing enrolled admission verifier. Neither
    interface replaces the native invocation gate/owner resource lock.
    """

    capability: InstalledOwnerCapability
    control_adapter: object
    admission_observer: object
    close: object


class RuntimeOwnerComposition:
    def __init__(self, config, registration):
        self.config, self.registration = config, registration
        self.closed = False
        if (
            type(registration) is not RuntimeOwnerRegistration
            or type(registration.capability) is not InstalledOwnerCapability
            or not callable(registration.capability.assert_current)
            or not callable(registration.close)
        ):
            raise NativeBridgeUnavailable("approved_runtime_owner_registration_required")
        self.assert_current()

    def assert_current(self):
        if self.closed:
            raise NativeBridgeUnavailable("runtime_owner_closed")
        # None is the enforcement convention: a Boolean never grants authority.
        try:
            enforced = self.registration.capability.assert_current()
        except Exception:
            raise NativeBridgeUnavailable("runtime_owner_current_linkage_unavailable") from None
        if enforced is not None:
            raise NativeBridgeUnavailable("runtime_owner_must_enforce_current_linkage")
        readback = self.registration.capability.readback
        if (
            type(readback) is not InstalledReadback
            or readback != self.config.expected_readback
            or readback.manifest_sha256 != self.config.owner.approved_manifest_sha256
            or readback.approval_reference != self.config.owner.approval_reference
        ):
            raise NativeBridgeUnavailable("runtime_owner_scope_or_pins_changed")

    def control_adapter(self):
        adapter = self.registration.control_adapter
        try:
            supported = all(
                callable(getattr(adapter, name, None))
                for name in ("verify_enrollment", "verify_allocation", "verify_claim")
            )
        except Exception:
            raise NativeBridgeUnavailable("runtime_control_adapter_unavailable") from None
        if not supported:
            raise NativeBridgeUnavailable("runtime_control_adapter_required")
        self.assert_current()
        return _CurrentControlAdapter(self, adapter)

    def admission_observer(self, ref, binding, capture, resources):
        from tessera_mcp.harness_observer import EvidenceBackedHarnessObserver

        self.assert_current()
        factory = self.registration.admission_observer
        if not callable(factory):
            raise NativeBridgeUnavailable("runtime_admission_observer_required")
        try:
            observer = factory(ref, binding, capture, resources)
        except Exception:
            raise NativeBridgeUnavailable("runtime_admission_observer_unavailable") from None
        self.assert_current()
        if type(observer) is not EvidenceBackedHarnessObserver:
            raise NativeBridgeUnavailable("enrolled_runtime_admission_observer_required")
        measured = self.config.expected_readback
        if (
            observer.verifier.pins.model_dump() != measured.pins.model_dump()
            or observer.verifier.scope.publisher_ref != measured.publisher_ref
            or observer.verifier.scope.opaque_owner_ref != measured.opaque_owner_ref
            or observer.verifier.scope.session_ref != measured.session_ref
            or observer.verifier.scope.credential != measured.subject_credential_id
            or observer.verifier.scope.credential != ref.credential_id
            or observer.verifier.scope.enrollment != ref.observer_admission_id
            or observer.binding.digest != measured.pins.binding
            or observer.binding.digest != binding.digest
        ):
            raise NativeBridgeUnavailable("runtime_observer_owner_binding_changed")

        def current_preparation():
            self.assert_current()
            try:
                result = observer.read_preparation()
            except Exception:
                raise NativeBridgeUnavailable("runtime_owner_preparation_unavailable") from None
            self.assert_current()
            return result

        return EvidenceBackedHarnessObserver(
            binding=observer.binding,
            verifier=observer.verifier,
            read_preparation=current_preparation,
        )

    def close(self):
        if not self.closed:
            self.closed = True
            try:
                self.registration.close()
            except Exception:
                raise NativeBridgeUnavailable("runtime_owner_close_failed") from None


class _CurrentControlAdapter:
    def __init__(self, owner, adapter):
        self.owner, self.adapter = owner, adapter

    def _verify(self, name, *args):
        self.owner.assert_current()
        try:
            result = getattr(self.adapter, name)(*args)
        except Exception:
            raise NativeBridgeUnavailable("runtime_owner_verification_unavailable") from None
        self.owner.assert_current()
        if name == "verify_claim":
            if type(result) is not NativeCheckWindow:
                raise NativeBridgeUnavailable("native_owner_check_window_required")
        elif result is not None:
            raise NativeBridgeUnavailable("runtime_owner_verification_must_enforce")
        return result

    def verify_enrollment(self, connection, actor, request, scope, pins):
        return self._verify("verify_enrollment", connection, actor, request, scope, pins)

    def verify_allocation(self, connection, actor, snapshot, request):
        return self._verify("verify_allocation", connection, actor, snapshot, request)

    def verify_claim(self, connection, actor, operation, request):
        return self._verify("verify_claim", connection, actor, operation, request)


def load_runtime_owner(config_file, approved_sha256, *, state_dir):
    """Load only exact approved bytes; no credential discovery or copying.

    open_runtime_owner(config, *, state_dir) is the portable owner entry point.
    Its private resource references remain with that approved owner. The config
    and adapter are bounded owner-private files. Signature verification remains
    mandatory inside capability.assert_current, not inferred from digest pins.
    """
    registration = None
    try:
        if not config_file or not approved_sha256:
            raise NativeBridgeUnavailable("explicit_runtime_owner_approval_required")
        state_dir = Path(state_dir)
        if not state_dir.is_absolute():
            raise NativeBridgeUnavailable("absolute_runtime_owner_state_required")
        raw = exact_file(config_file, private=True)
        if hashlib.sha256(raw).hexdigest() != approved_sha256:
            raise NativeBridgeUnavailable("runtime_owner_config_digest_changed")
        config = RuntimeOwnerConfig.model_validate(json.loads(raw, object_pairs_hook=closed_object))
        if (
            config.expected_readback.manifest_sha256 != config.owner.approved_manifest_sha256
            or config.expected_readback.approval_reference != config.owner.approval_reference
        ):
            raise NativeBridgeUnavailable("runtime_owner_approval_binding_changed")
        manifest = exact_file(config.owner.signed_manifest_file, limit=4 * 1024 * 1024)
        if hashlib.sha256(manifest).hexdigest() != config.owner.approved_manifest_sha256:
            raise NativeBridgeUnavailable("runtime_owner_manifest_digest_changed")
        code = exact_file(config.owner.adapter_file, private=True, limit=1024 * 1024)
        if hashlib.sha256(code).hexdigest() != config.owner.adapter_sha256:
            raise NativeBridgeUnavailable("runtime_owner_adapter_digest_changed")
        # Inspect ownership/layout only through the existing bounded file check;
        # contents are never interpreted as a permit or forwarded to logs.
        exact_file(config.owner.resource_reference_file, private=True)
        spec = importlib.util.spec_from_file_location(
            "tessera_explicit_runtime_owner", config.owner.adapter_file
        )
        module = importlib.util.module_from_spec(spec)
        exec(compile(code, config.owner.adapter_file, "exec"), module.__dict__)
        registration = module.open_runtime_owner(config, state_dir=state_dir)
        return RuntimeOwnerComposition(config, registration)
    except Exception:
        if type(registration) is RuntimeOwnerRegistration and callable(registration.close):
            try:
                registration.close()
            except Exception:
                pass
        # Never leak module errors, native output, private paths or credentials.
        raise NativeBridgeUnavailable("approved_runtime_owner_configuration_required") from None


class ResearchOwnerComposition:
    """Independent planner/critic capabilities; no shared account-owner proxy."""

    def __init__(self, owners):
        self.owners = owners

    def admission_observer(self, ref, binding, capture, resources):
        matches = [
            owner
            for owner in self.owners
            if owner.config.expected_readback.subject_credential_id == ref.credential_id
        ]
        if len(matches) != 1:
            raise NativeBridgeUnavailable("independent_research_owner_binding_required")
        return matches[0].admission_observer(ref, binding, capture, resources)

    def close(self):
        failed = False
        for owner in self.owners:
            try:
                owner.close()
            except Exception:
                failed = True
        if failed:
            raise NativeBridgeUnavailable("runtime_owner_close_failed")


def load_research_runtime_owner(config_file, approved_sha256, *, state_dir):
    owners = []
    try:
        if not config_file or not approved_sha256:
            raise NativeBridgeUnavailable("explicit_research_owner_approval_required")
        raw = exact_file(config_file, private=True)
        if hashlib.sha256(raw).hexdigest() != approved_sha256:
            raise NativeBridgeUnavailable("research_owner_config_digest_changed")
        config = ResearchOwnerConfig.model_validate(
            json.loads(raw, object_pairs_hook=closed_object)
        )
        if config.planner.config_file == config.critic.config_file:
            raise NativeBridgeUnavailable("independent_research_owner_configuration_required")
        for role in (config.planner, config.critic):
            owners.append(
                load_runtime_owner(role.config_file, role.approved_sha256, state_dir=state_dir)
            )
        if (
            owners[0].config.owner.resource_reference_file
            == owners[1].config.owner.resource_reference_file
        ):
            raise NativeBridgeUnavailable("independent_research_owner_resources_required")
        for name in ("subject_credential_id", "session_ref", "opaque_owner_ref", "publisher_ref"):
            if getattr(owners[0].config.expected_readback, name) == getattr(
                owners[1].config.expected_readback, name
            ):
                raise NativeBridgeUnavailable("independent_research_owner_binding_required")
        return ResearchOwnerComposition(owners)
    except Exception:
        for owner in owners:
            try:
                owner.close()
            except Exception:
                pass
        raise NativeBridgeUnavailable("approved_research_owner_configuration_required") from None
