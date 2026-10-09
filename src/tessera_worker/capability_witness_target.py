"""Explicit deployment pins for live witnesses; configuration is not evidence."""

from __future__ import annotations

import hashlib
import ssl
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from tessera_control.capability_models import Digest, Identifier

from .capability_activation import Frozen, Name, canonical
from .capability_collector_cli import CredentialReference, RuntimeWitnessConformanceConfig
from .capability_kube_client import (
    CollectionError,
    HTTPSKubernetesTransport,
    load_credentials,
    private_read,
)
from .capability_witness import KINDS, WitnessPin, require


class MutualTLS(Frozen):
    # Runtime references only. No generated credentials or system trust store.
    certificate_file: str = Field(max_length=1024)
    private_key_file: str = Field(max_length=1024)
    certificate_sha256: Digest  # DER fingerprint, not PEM formatting
    peer_certificate_sha256: Digest

    @model_validator(mode="after")
    def absolute(self):
        if not all(Path(p).is_absolute() for p in (self.certificate_file, self.private_key_file)):
            raise ValueError("absolute mTLS references required")
        return self


def mtls_transport(ref, tls):
    """Load explicit credentials only after live opt-in, without printing errors."""
    try:
        credentials = load_credentials(
            ref.binding, kubeconfig=ref.kubeconfig, token_file=ref.token_file, ca_file=ref.ca_file
        )
        transport = HTTPSKubernetesTransport(ref.binding, credentials)
        cert = private_read(tls.certificate_file, 128 * 1024)
        key = private_read(tls.private_key_file, 128 * 1024)
        der = ssl.PEM_cert_to_DER_cert(cert.decode("ascii"))
        require(hashlib.sha256(der).hexdigest() == tls.certificate_sha256)
        # OpenSSL requires filenames. Verify contents again after loading; deployment
        # custody must exclude concurrent credential writers. Never create temp keys.
        transport._tls.load_cert_chain(tls.certificate_file, tls.private_key_file)
        require(private_read(tls.certificate_file, 128 * 1024) == cert)
        require(private_read(tls.private_key_file, 128 * 1024) == key)
        transport.peer_certificate_sha256 = tls.peer_certificate_sha256
        return transport
    except Exception:
        raise CollectionError("auth-denied") from None


class NativeChannel(Frozen):
    credentials: CredentialReference
    tls: MutualTLS
    request_key_file: str = Field(max_length=1024)
    response_key_file: str = Field(max_length=1024)

    @model_validator(mode="after")
    def absolute(self):
        if not all(Path(p).is_absolute() for p in (self.request_key_file, self.response_key_file)):
            raise ValueError("absolute native key references required")
        if self.request_key_file == self.response_key_file:
            raise ValueError("separate native keys required")
        return self


class TargetProfile(Frozen):
    version: Literal[1] = 1
    platform: Literal["docker-desktop-kubernetes"]
    context: Literal["docker-desktop"]
    cluster_digest: Digest
    plan_digest: Digest
    node_name: Name
    node_uid: Identifier
    boot_id: Identifier
    kubernetes_version: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
    # This version approves NO default removal. A target with native defaults blocks.
    admission_profile: Literal["exact-no-unreviewed-defaults-v1"]
    fixture_protocol: Literal["nonce-owned-disposable-v1"]
    image: str = Field(pattern=r"^[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}$")
    # Reviewed already-present fixture binary, NOT an image download permission.
    fixture_binary_sha256: Digest
    probe_service_accounts: dict[str, tuple[Name, Identifier]] = Field(min_length=8, max_length=8)
    endpoint_service_accounts: dict[str, tuple[Name, Identifier]] = Field(
        min_length=8, max_length=8
    )
    cni_driver: Literal["native-policy-flow-v1", "unavailable"]
    storage_driver: Literal["linux-project-quota-v1", "unavailable"]
    cri_driver: Literal["containerd-cri-v1"]
    max_calls: int = Field(ge=1, le=128)
    max_bytes: int = Field(ge=1024, le=2 * 1024 * 1024)
    max_seconds: int = Field(ge=1, le=5)
    max_resources: int = Field(ge=1, le=32)
    retries: Literal[0]

    @model_validator(mode="after")
    def fixture_identities(self):
        categories = {
            "allowed",
            "allowed-dns",
            "public",
            "private",
            "metadata",
            "control",
            "dns-bypass",
            "proxy",
        }
        for accounts in (self.probe_service_accounts, self.endpoint_service_accounts):
            if set(accounts) != categories or len({v[0] for v in accounts.values()}) != 8:
                raise ValueError("eight exact distinct fixture ServiceAccounts required")
            if len({v[1] for v in accounts.values()}) != 8 or any(
                v[0] == "default" for v in accounts.values()
            ):
                raise ValueError("distinct non-default fixture identities required")
        return self

    @property
    def digest(self):
        return hashlib.sha256(canonical(self)).hexdigest()


class LiveWitnessTarget(Frozen):
    """Daemon-owned composition, never accepted from a workload challenge."""

    profile: TargetProfile
    pin: WitnessPin
    api: CredentialReference
    supervisor: NativeChannel

    @model_validator(mode="after")
    def bound(self):
        p, pin, b = self.profile, self.pin, self.api.binding
        if (
            pin.target_digest != p.digest
            or (pin.node_uid, pin.boot_id) != (p.node_uid, p.boot_id)
            or b.cluster_digest != p.cluster_digest
            or b.plan_digest != p.plan_digest
            or b.context != p.context
            or b.identity != pin.identity
            or (b.namespace, b.namespace_uid) != (pin.namespace, pin.namespace_uid)
            or self.supervisor.credentials.binding.plan_digest != p.plan_digest
            or self.supervisor.credentials.binding.identity != pin.identity
        ):
            raise ValueError("exact target/observer binding required")
        return self


class TargetConformanceConfig(RuntimeWitnessConformanceConfig):
    target: TargetProfile
    witness_tls: dict[str, MutualTLS] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def target_bound(self):
        p, m = self.target, self.plan.activation
        if (
            p.plan_digest != self.plan.digest
            or p.cluster_digest != m.cluster_digest
            or p.image != m.image
            or set(self.witness_tls) != set(KINDS)
            or self.collector.binding.context != p.context
        ):
            raise ValueError("exact target required")
        for kind, ref in self.witnesses.items():
            if ref.pin.target_digest != p.digest or (ref.pin.node_uid, ref.pin.boot_id) != (
                p.node_uid,
                p.boot_id,
            ):
                raise ValueError("witness target mismatch")
            if kind == "watchdog" and (
                ref.pin.identity != m.watchdog.identity
                or ref.channel.binding.server != m.watchdog.endpoint
                or self.witness_tls[kind].peer_certificate_sha256 != m.watchdog.certificate_sha256
            ):
                raise ValueError("watchdog endpoint/certificate mismatch")
        if len({r.pin.identity for r in self.witnesses.values()}) != 4:
            raise ValueError("four independent identities required")
        if len({t.peer_certificate_sha256 for t in self.witness_tls.values()}) != 4:
            raise ValueError("four independent service certificates required")
        return self
