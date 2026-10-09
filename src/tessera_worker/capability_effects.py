"""Synthetic bounded transport and a deliberately blocked real-backend boundary.

No shell, subprocess, HTTP library, credential lookup, container engine or provider
is used. A policy object is never evidence of runtime isolation.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import ConfigDict

from tessera_control.capability_models import Strict

CHUNK_BYTES = 64 * 1024


class BackendBlocked(ValueError):
    pass


@dataclass(frozen=True)
class DownloadResponse:
    url: str
    addresses: tuple[str, ...]
    status: int
    content_type: str
    size: int


@dataclass(frozen=True)
class InstallResult:
    inventory: tuple[tuple[str, str, str], ...]
    exit_code: int
    verified: bool
    termination_confirmed: bool
    target_digest: str


class CapabilityEffects(Protocol):
    """Supervisor-injected transport, never an agent-selected implementation.

    Live implementations must enforce timeout outside the caller thread, bind all
    methods to one sandbox, forbid redirects/proxies, and pin the supplied IPs.
    No live implementation is provided or selected by this protocol.
    """

    def preflight(self, body) -> None: ...
    def resolve(self, body, *, timeout: float) -> tuple[str, ...]: ...
    def open(self, body, *, addresses: tuple[str, ...], timeout: float) -> DownloadResponse: ...
    def read(self, offset: int, *, limit: int, timeout: float) -> bytes: ...
    def install(
        self, body, lock: bytes, *, target_digest: str, timeout: float
    ) -> InstallResult: ...
    def stop(self, *, timeout: float) -> bool: ...


class FakeCapabilityEffects:
    """Explicit fixture bytes only. Test callbacks can model faults between steps."""

    def __init__(self, content=b"", *, content_type="application/octet-stream"):
        self.content = content
        self.content_type = content_type
        self.calls = []
        self.before = lambda phase: None
        self.response = None
        self.install_result = None
        self.confirm_stop = True

    def preflight(self, body):
        if body.mode != "synthetic":
            raise BackendBlocked("fake transport cannot satisfy real authority")

    def resolve(self, body, *, timeout):
        self.before("resolve")
        self.calls.append("resolve")
        return self.response.addresses if self.response else ("8.8.8.8",)

    def open(self, body, *, addresses, timeout):
        self.before("open")
        self.calls.append("open")
        return self.response or DownloadResponse(
            body.binding.action.url, ("8.8.8.8",), 200, self.content_type, len(self.content)
        )

    def read(self, offset, *, limit, timeout):
        self.before("read")
        self.calls.append("read")
        return self.content[offset : offset + limit]

    def install(self, body, lock, *, target_digest, timeout):
        self.before("install")
        self.calls.append("install")
        action = body.binding.action
        return self.install_result or InstallResult(
            ((action.name, action.version, action.sha256),), 0, True, True, target_digest
        )

    def stop(self, *, timeout):
        self.before("stop")
        self.calls.append("stop")
        return self.confirm_stop


class RealBackendConfig(Strict):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    enabled: bool = False
    target: Literal["local", "kubernetes"]
    # Names in a trusted deployment-owned registry, NOT executables or agent args.
    sandbox_runner_id: str
    network_client_id: str
    isolation_inspector_id: str


class GatedRealCapabilityEffects:
    """Activation boundary, intentionally no supplied live implementation.

    Future implementations must use only explicitly injected sandbox runner and
    pinned-IP TLS client, with independently observed isolation, external watchdog,
    quotas, and authenticated global dedup. A boolean evidence object cannot
    establish these facts. No current inspector can prove the entire contract.
    """

    def __init__(
        self,
        config: RealBackendConfig,
        *,
        sandbox_runner=None,
        network_client=None,
        isolation_inspector=None,
    ):
        self.config = config
        self.sandbox_runner = sandbox_runner
        self.network_client = network_client
        self.isolation_inspector = isolation_inspector

    def preflight(self, body):
        raise BackendBlocked("local/Kubernetes trusted isolation activation is unavailable")
