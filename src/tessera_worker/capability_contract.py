"""Requirements for a future trusted artifact broker, NOT a download/install adapter.

The control-plane response alone cannot enable execution. Existing adapters do not
implement this contract. Evidence must come from trusted runtime inspection, never
agent JSON. No socket, filesystem write, process or secret access occurs here.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from ipaddress import ip_address
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from tessera_control.capability_models import (
    AdmitCapability,
    artifact_url,
    fenced_root,
    public_address,
)


@dataclass(frozen=True)
class FenceEvidence:
    environment_id: str
    image_digest: str
    lease_id: str
    epoch: int
    # All observations are false until independently established by an adapter.
    isolated_mount_namespace: bool = False
    no_host_or_control_mounts: bool = False
    no_ambient_credentials: bool = False
    no_privileged_or_host_network: bool = False
    broker_only_egress: bool = False
    non_root_readonly_image: bool = False
    byte_time_limits: bool = False
    no_symlink_or_archive_escape: bool = False
    offline_hash_locked_installer: bool = False
    durable_operation_deduplication: bool = False

    def require(self, binding: AdmitCapability) -> None:
        if type(self.epoch) is not int or (
            self.environment_id,
            self.image_digest,
            self.lease_id,
            self.epoch,
        ) != (
            binding.environment_id,
            binding.image_digest,
            binding.execution_lease_id,
            binding.expected_epoch,
        ):
            raise ValueError("runtime identity/lease/epoch differs from approved capability")
        flags = (
            self.isolated_mount_namespace,
            self.no_host_or_control_mounts,
            self.no_ambient_credentials,
            self.no_privileged_or_host_network,
            self.broker_only_egress,
            self.non_root_readonly_image,
            self.byte_time_limits,
            self.no_symlink_or_archive_escape,
            self.offline_hash_locked_installer,
            self.durable_operation_deduplication,
        )
        if any(flag is not True for flag in flags):
            raise ValueError("runtime cannot enforce the fenced capability contract")


def check_broker_connection(binding: AdmitCapability, url: str, resolved_ips: list[str]) -> None:
    """Check every connection; adapters must pin the checked IP and verify TLS host.

    Redirects, proxies from environment, DNS rebinding, private/metadata endpoints,
    uploads and alternate registries are not supported. HTTPS GET only.
    """
    artifact_url(url)
    if url != binding.action.url or not resolved_ips:
        raise ValueError("unapproved destination or unresolved address")
    if any(not public_address(ip_address(address)) for address in resolved_ips):
        raise ValueError("private/metadata/special-use address denied")
    if urlsplit(url).scheme != "https":
        raise ValueError("TLS required")


def check_materialization(binding: AdmitCapability, path: str) -> None:
    """Lexical check only; runtime must also use no-follow directory-fd operations."""
    fenced_root(path)
    target = PurePosixPath(path)
    if (
        str(target) != path
        or ".." in target.parts
        or not target.is_relative_to(binding.action.filesystem_root)
    ):
        raise ValueError("materialization path escapes the approved sandbox root")


def check_authority_window(authority_until: str) -> None:
    deadline = datetime.fromisoformat(authority_until.replace("Z", "+00:00"))
    now = datetime.now(UTC)
    if deadline.tzinfo is None or not 0 < (deadline - now).total_seconds() <= 5:
        raise ValueError("missing, expired or overlong capability authority window")


def require_effect_authority(
    response: dict, operation_id: str, binding: AdmitCapability, fence: FenceEvidence
) -> None:
    """Compose mandatory gates; current control responses always fail this gate.

    Only a fresh authenticated control response is eligible input. An adapter must
    durably claim operation_id once, recheck before each connection/install and at
    least every five seconds, and kill/quarantine on failure or loss of control.
    Agent-supplied response or evidence dictionaries must never reach this boundary.
    """
    if (
        response.get("execution_enabled") is not True
        or response.get("operation_id") != operation_id
        or response.get("binding") != binding.model_dump(mode="json")
    ):
        raise ValueError("capability effects are disabled or authority binding differs")
    fence.require(binding)
    check_authority_window(response["authority_until"])


def check_download_bytes(binding: AdmitCapability, content: bytes) -> None:
    """Pure post-download check; a broker must separately bound bytes while streaming."""
    if (
        len(content) > binding.action.max_bytes
        or sha256(content).hexdigest() != binding.action.sha256
    ):
        raise ValueError("download exceeds budget or differs from pinned digest")


def check_lockfile_bytes(binding: AdmitCapability, content: bytes) -> None:
    """Check a staged lock before any offline install; this does not parse a lock.

    The adapter must also prove the full transitive closure equals the approved
    package actions, use no-dependency/no-index mode, and reject executable build
    hooks/source distributions or unsupported lock formats. No shell is authorized.
    """
    if (
        binding.action.kind != "package"
        or sha256(content).hexdigest() != binding.action.lockfile_sha256
    ):
        raise ValueError("missing or mismatched approved package lock")
