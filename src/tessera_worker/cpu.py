"""Docker CPU boundary with stable operation identity and explicit uncertainty.

Only the trusted supervisor may construct an operation from an active grant.
Only explicitly verified public inputs may be mounted, read-only, into an experiment.
Docker sockets, credentials and other host paths are never mounted.
This adapter does not infer scientific success from a container exit code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import subprocess
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from tessera_control import artifact_limits as artifact_limits_module

from . import scratch_limits as limits
from .egress_proxy import journal_sha256, summarize_journal

if TYPE_CHECKING:
    from .docker_network_pool import DockerNetworkPool

_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}\Z")
_OPERATION = re.compile(r"[a-f0-9]{32}\Z")
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
# Historical default bound; the operator-configurable bound is DockerCpuAdapter.max_input_bytes.
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_FILES = 1000
MAX_INPUT_MANIFEST_BYTES = 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


log = logging.getLogger("tessera.worker.cpu")


class CpuError(Exception):
    """A provider operation could not be confirmed."""


class CpuLaunchError(CpuError):
    """Launching was refused before any container could exist (ADR 0046).

    ``launch_error`` is one of control's closed launch-error classes. It is raised only
    where Docker returned a definite refusal and a fresh read shows no container, never for
    a lost reply or timeout, which stay unconfirmed."""

    def __init__(self, message: str, launch_error: str) -> None:
        super().__init__(message)
        self.launch_error = launch_error


def launch_error_class(stderr: str, *, network: bool) -> str:
    """Closed class for a definite Docker refusal; only the class leaves the worker."""
    text = stderr.lower()
    if network:
        if "address pools" in text:
            return "network_address_pool_exhausted"
        return "network_create_failed"
    if "no such image" in text or "pull access denied" in text or "manifest unknown" in text:
        return "image_unavailable"
    return "container_create_failed"


class NetworkCreateError(CpuLaunchError):
    """``docker network create`` failed; no container exists for this operation yet.

    ``docker_error`` (bounded Docker text) stays a local diagnostic; only the closed
    ``launch_error`` class is reported to control (ADR 0046)."""

    def __init__(self, message: str, docker_error: str = "") -> None:
        super().__init__(message, launch_error_class(docker_error, network=True))
        self.docker_error = docker_error


class SubnetPoolExhausted(NetworkCreateError):
    """No free subnet remains in the operator's configured Docker network pool."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.launch_error = "network_address_pool_exhausted"


# Identity the stage containers have always run as; the image build's own user.
LEGACY_CONTAINER_USER = "65532:65532"
_CONTAINER_USER = re.compile(r"(0|[1-9][0-9]{0,9}):(0|[1-9][0-9]{0,9})\Z")
_MAX_ID = 2**32 - 2


def validate_container_user(value: Any, *, allow_root: bool = False) -> str:
    """Return a numeric ``uid:gid`` container user or raise ValueError.

    Names are refused (they would resolve inside the untrusted image). uid 0 or gid 0 is
    refused unless the operator explicitly allows it.
    """
    match = _CONTAINER_USER.fullmatch(value) if isinstance(value, str) else None
    if match is None or any(int(part) > _MAX_ID for part in match.groups()):
        raise ValueError("Container user must be a numeric uid:gid.")
    if not allow_root and "0" in (match.group(1), match.group(2)):
        raise ValueError("Container user uid/gid 0 requires an explicit operator override.")
    return value


def default_container_user() -> str | None:
    """The default operator container user.

    On native Linux the 0700 output workspace is owned by the poller, so containers run as
    the poller's own uid:gid. Elsewhere (Docker Desktop maps bind-mount ownership) and for a
    root poller the existing 65532:65532 behaviour is kept; a root poller must configure a
    user explicitly.
    """
    if not sys.platform.startswith("linux"):
        return None
    uid, gid = os.geteuid(), os.getegid()
    return f"{uid}:{gid}" if uid != 0 and gid != 0 else None


EGRESS_PROFILE = "allowlist-proxy-v1"
PROXY_PORT = 3128
DEVELOPMENT_OUTPUT_PROFILE = "development-local-cpu-v1"
DEVELOPMENT_FIXTURE_MARKER = "development-local-fixture-v1"


@dataclass(frozen=True)
class DevelopmentOutputMount:
    """An explicit, non-pilot output mount identity.

    This marker is deliberately a typed development profile, not a feature flag.
    Normal CPU operations cannot acquire an output bind mount.
    """

    directory: str
    profile: Literal["development-local-cpu-v1"] = DEVELOPMENT_OUTPUT_PROFILE
    fixture_marker: Literal["development-local-fixture-v1"] = DEVELOPMENT_FIXTURE_MARKER

    def __post_init__(self) -> None:
        if (
            not isinstance(self.directory, str)
            or not self.directory
            or self.profile != DEVELOPMENT_OUTPUT_PROFILE
            or self.fixture_marker != DEVELOPMENT_FIXTURE_MARKER
        ):
            raise ValueError("Development output mount requires the explicit fixture profile.")


@dataclass(frozen=True)
class CpuOperation:
    operation_id: str
    image: str
    argv: tuple[str, ...]
    plan_digest: str
    max_duration_seconds: int
    memory_bytes: int = limits.DEFAULT_MEMORY_BYTES
    scratch_bytes: int = limits.DEFAULT_SCRATCH_BYTES
    # Operator-owned (never from a plan or spec); recorded in the durable fence.
    container_user: str = LEGACY_CONTAINER_USER
    input_directory: str | None = None
    input_manifest_json: str | None = None
    development_output: DevelopmentOutputMount | None = None
    network_profile: str = "none"
    egress: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.network_profile == "none":
            if self.egress is not None:
                raise ValueError("Network profile none cannot carry an egress policy.")
        elif self.network_profile == EGRESS_PROFILE:
            policy = self.egress
            destinations = policy.get("destinations") if isinstance(policy, dict) else None
            if (
                not isinstance(policy, dict)
                or policy.get("profile") != EGRESS_PROFILE
                or not isinstance(destinations, list)
                or not 1 <= len(destinations) <= 32
                or any(
                    not isinstance(item, dict)
                    or not isinstance(item.get("host"), str)
                    or not item["host"]
                    or type(item.get("port")) is not int
                    or not 1 <= item["port"] <= 65535
                    for item in destinations
                )
                or type(policy.get("max_egress_bytes")) is not int
                or policy["max_egress_bytes"] < 1
            ):
                raise ValueError("Allowlist egress profile requires a valid egress policy.")
        else:
            raise ValueError("Unknown network profile.")
        if not _OPERATION.fullmatch(self.operation_id) or not _IMAGE.fullmatch(self.image):
            raise ValueError("Stable operation ID and digest-pinned image are required.")
        if not re.fullmatch(r"[a-f0-9]{64}", self.plan_digest):
            raise ValueError("Plan digest is required.")
        if (
            not self.argv
            or len(self.argv) > 128
            or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 4096 for arg in self.argv)
        ):
            raise ValueError("Invalid command arguments.")
        if not 1 <= self.max_duration_seconds <= 86400:
            raise ValueError("Invalid execution duration.")
        if not limits.MIN_MEMORY_BYTES <= self.memory_bytes <= limits.MAX_BYTES:
            raise ValueError("Invalid memory bound.")
        if not limits.MIN_SCRATCH_BYTES <= self.scratch_bytes <= limits.MAX_BYTES:
            raise ValueError("Invalid scratch bound.")
        if not limits.scratch_fits_memory(self.scratch_bytes, self.memory_bytes):
            # tmpfs is charged to the container's memory, so a scratch that cannot fit in
            # it would end as an out-of-memory kill, not as a full scratch.
            raise ValueError("Scratch needs more memory than the container's memory limit.")
        validate_container_user(self.container_user, allow_root=True)
        if (self.input_directory is None) != (self.input_manifest_json is None):
            raise ValueError("Input directory and manifest must be supplied together.")
        if self.input_directory is not None:
            if not self.input_directory or not isinstance(self.input_manifest_json, str):
                raise ValueError("Invalid input identity.")
            try:
                canonical = json.dumps(
                    json.loads(self.input_manifest_json), sort_keys=True, separators=(",", ":")
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError("Invalid input manifest.") from error
            if canonical != self.input_manifest_json or len(canonical.encode()) > 1024 * 1024:
                raise ValueError("Input manifest must be canonical and bounded.")

    @property
    def container_name(self) -> str:
        return f"tessera-op-{self.operation_id}"

    @property
    def egress_network_name(self) -> str:
        return f"tessera-egress-{self.operation_id}"

    @property
    def egress_proxy_name(self) -> str:
        return f"tessera-egress-proxy-{self.operation_id}"


@dataclass(frozen=True)
class CpuObservation:
    state: str  # absent, created, running, exited, unknown
    container_id: str | None = None
    exit_code: int | None = None
    termination_confirmed: bool = False


RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _docker(arguments: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # Inspect/create/kill have bounded small responses; experiment logs are not
    # captured here. They need a separate bounded artifact collection path.
    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CpuError("Docker reply unavailable; reconcile by operation ID.") from error
    if len(result.stdout.encode()) + len(result.stderr.encode()) > MAX_CAPTURE_BYTES:
        raise CpuError("Docker response exceeded its bound.")
    return result


class DockerCpuAdapter:
    """Admit trusted image digests and explicitly configured read-only input mounts.

    The supervisor must persist operation intent before create and enforce the
    monotonic wall-clock deadline/disconnect policy while the container runs.
    A lost Docker reply is reconciled with inspect, never a fresh container name.
    """

    def __init__(
        self,
        allowed_images: frozenset[str],
        run: RunCommand = _docker,
        input_root: Path | None = None,
        development_output_root: Path | None = None,
        egress_proxy_image: str | None = None,
        egress_root: Path | None = None,
        egress_proxy_script: Path | None = None,
        memory_bytes: int | None = None,
        scratch_bytes: int | None = None,
        container_user: str | None = None,
        allow_root_container_user: bool = False,
        network_pool: DockerNetworkPool | None = None,
        max_input_bytes: int | None = None,
        max_scratch_bytes: int | None = None,
    ):
        # Operator-owned subnet pool for Tessera-created networks; None keeps Docker's
        # default address pools (the historical behaviour).
        self.network_pool = network_pool
        # Operator-owned bound on the total (and per-file) input bytes of one operation; None
        # keeps the installation default. Inputs are verified by streaming, never loaded whole.
        if max_input_bytes is None:
            max_input_bytes = artifact_limits_module.operator_limits().max_artifact_bytes
        if (
            type(max_input_bytes) is not int
            or not artifact_limits_module.LEGACY_INLINE_BYTES
            <= max_input_bytes
            <= artifact_limits_module.ABSOLUTE_MAX_ARTIFACT_BYTES
        ):
            raise ValueError("Invalid input byte bound.")
        self.max_input_bytes = max_input_bytes
        # Operator-owned container limits (never plan-controlled); None keeps the CpuOperation
        # defaults. Same bounds as CpuOperation. operation_limits() hands them to the runtime.
        if memory_bytes is not None and not limits.valid_bytes(
            memory_bytes, limits.MIN_MEMORY_BYTES, limits.MAX_BYTES
        ):
            raise ValueError("Invalid memory bound.")
        if scratch_bytes is not None and not limits.valid_bytes(
            scratch_bytes, limits.MIN_SCRATCH_BYTES, limits.MAX_BYTES
        ):
            raise ValueError("Invalid scratch bound.")
        # Operator ceiling on the scratch a plan may request (ADR 0059): the historical
        # 64 MiB unless the operator raises it, and never below the default scratch.
        ceiling = (
            max_scratch_bytes
            if max_scratch_bytes is not None
            else max(limits.LEGACY_SCRATCH_CEILING, scratch_bytes or 0)
        )
        if not limits.valid_bytes(ceiling, limits.MIN_SCRATCH_BYTES, limits.MAX_BYTES) or (
            scratch_bytes is not None and scratch_bytes > ceiling
        ):
            raise ValueError("Invalid scratch ceiling.")
        if scratch_bytes is not None and not limits.scratch_fits_memory(
            scratch_bytes, memory_bytes
        ):
            # The default scratch every container gets must itself fit the memory limit, or
            # every grant would fail when its operation is built.
            raise ValueError("Scratch needs more memory than the container's memory limit.")
        limits.check_host_capacity(memory_bytes, ceiling)
        backed = limits.scratch_backed_by(memory_bytes)
        if ceiling > max(backed, limits.LEGACY_SCRATCH_CEILING):
            # A request between what the memory can back and the ceiling passes control's
            # admission (which knows only the ceiling) and is refused here, at run.
            log.warning(
                "max_scratch_bytes %d is above the %d bytes memory_bytes can back; larger "
                "requests are refused when their operation is built",
                ceiling,
                backed,
            )
        self.memory_bytes = memory_bytes
        self.scratch_bytes = scratch_bytes
        self.max_scratch_bytes = ceiling
        # Operator-owned container identity; None keeps the CpuOperation default.
        self.allow_root_container_user = allow_root_container_user is True
        self.container_user = (
            None
            if container_user is None
            else validate_container_user(container_user, allow_root=self.allow_root_container_user)
        )
        if egress_proxy_image is not None and not _IMAGE.fullmatch(egress_proxy_image):
            raise ValueError("The egress proxy image must be digest pinned.")
        self.egress_proxy_image = egress_proxy_image
        self.egress_proxy_script = (
            Path(egress_proxy_script)
            if egress_proxy_script is not None
            else Path(__file__).with_name("egress_proxy.py")
        )
        self.egress_root = (
            self._private_input_root(egress_root) if egress_root is not None else None
        )
        if not allowed_images or any(not _IMAGE.fullmatch(image) for image in allowed_images):
            raise ValueError("At least one pinned trusted image is required.")
        self.allowed_images = allowed_images
        self.run = run
        self.input_root = self._private_input_root(input_root) if input_root is not None else None
        if self.egress_root is None:
            self.egress_root = self.input_root
        self.development_output_root = (
            self._private_input_root(development_output_root)
            if development_output_root is not None
            else None
        )

    @staticmethod
    def _private_input_root(root: Path) -> Path:
        """Return a stable, private supervisor-owned root for bind mounts."""
        root = Path(root)
        try:
            mode = root.lstat().st_mode
            resolved = root.resolve(strict=True)
        except OSError as error:
            raise ValueError("Input root must be an existing private directory.") from error
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode) or stat.S_IMODE(mode) != 0o700:
            raise ValueError("Input root must be a private 0700 directory.")
        return resolved

    @staticmethod
    def _relative_path(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value.encode()) > 4096
            or len(value.split("/")) > 64
            or "\\" in value
            or "\0" in value
            or re.match(r"^[A-Za-z]:", value)
            or any(part in {"", ".", ".."} or len(part.encode()) > 255 for part in value.split("/"))
        ):
            raise CpuError("Input manifest contains an unsafe relative path.")
        return value

    @classmethod
    def _manifest_entries(
        cls, encoded: str, max_input_bytes: int = MAX_INPUT_BYTES
    ) -> tuple[dict[str, dict[str, Any]], set[str], str]:
        """Validate the frozen canonical manifest before every provider action."""
        try:
            if len(encoded.encode()) > MAX_INPUT_MANIFEST_BYTES:
                raise ValueError
            manifest = json.loads(encoded)
            canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            if canonical != encoded or not isinstance(manifest, dict):
                raise ValueError
            manifest_digest = hashlib.sha256(canonical.encode()).hexdigest()
            if (
                set(manifest) != {"root", "entries", "exclusions", "total_bytes"}
                or not isinstance(manifest["root"], str)
                or not isinstance(manifest["entries"], list)
                or not isinstance(manifest["exclusions"], list)
                or len(manifest["entries"]) > MAX_INPUT_FILES
                or len(manifest["exclusions"]) > 10000
                or type(manifest["total_bytes"]) is not int
                or not 0 <= manifest["total_bytes"] <= max_input_bytes
            ):
                raise ValueError
            expected: dict[str, dict[str, Any]] = {}
            total = 0
            for entry in manifest["entries"]:
                if not isinstance(entry, dict) or set(entry) != {
                    "relative_path",
                    "size",
                    "sha256",
                    "provenance",
                    "classification",
                }:
                    raise ValueError
                path = cls._relative_path(entry["relative_path"])
                if (
                    path in expected
                    or type(entry["size"]) is not int
                    or not 0 <= entry["size"] <= max_input_bytes
                    or not isinstance(entry["sha256"], str)
                    or not _DIGEST.fullmatch(entry["sha256"])
                    or entry["provenance"] != "imported_unverified"
                    or entry["classification"] != "unclassified"
                ):
                    raise ValueError
                total += entry["size"]
                if total > max_input_bytes:
                    raise ValueError
                expected[path] = entry
            if total != manifest["total_bytes"]:
                raise ValueError
            if any(
                not isinstance(exclusion, dict)
                or set(exclusion) != {"relative_path", "reason"}
                or not all(isinstance(value, str) for value in exclusion.values())
                for exclusion in manifest["exclusions"]
            ):
                raise ValueError
            directories: set[str] = set()
            aliases: dict[str, str] = {}
            for path in expected:
                parts = path.split("/")
                for length in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:length])
                    alias = unicodedata.normalize("NFC", prefix).casefold()
                    if alias in aliases and aliases[alias] != prefix:
                        raise ValueError
                    aliases[alias] = prefix
                    if length != len(parts):
                        directories.add(prefix)
            if directories.intersection(expected):
                raise ValueError
            return expected, directories, manifest_digest
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as error:
            raise CpuError("Input manifest is not a valid frozen snapshot manifest.") from error

    def _assert_private_root(self) -> None:
        assert self.input_root is not None
        try:
            mode = self.input_root.lstat().st_mode
            resolved = self.input_root.resolve(strict=True)
        except OSError as error:
            raise CpuError("Input mount root is unavailable.") from error
        if (
            stat.S_ISLNK(mode)
            or not stat.S_ISDIR(mode)
            or stat.S_IMODE(mode) != 0o700
            or resolved != self.input_root
        ):
            raise CpuError("Input mount root is no longer private supervisor custody.")

    def _input_mount(self, operation: CpuOperation) -> list[str]:
        if operation.input_directory is None:
            return []
        if self.input_root is None:
            raise CpuError("Input mount root is not configured.")
        self._assert_private_root()
        directory = Path(operation.input_directory)
        if not directory.is_absolute():
            raise CpuError("Input directory must be an absolute trusted path.")
        try:
            relative = directory.relative_to(self.input_root)
            current = self.input_root
            for part in relative.parts:
                current /= part
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ValueError("symlink")
            resolved = directory.resolve(strict=True)
        except (OSError, ValueError) as error:
            raise CpuError("Input directory escapes trusted supervisor custody.") from error
        if directory == self.input_root or resolved != directory:
            raise CpuError("Input directory is not an admitted snapshot.")
        expected, expected_directories, manifest_digest = self._manifest_entries(
            operation.input_manifest_json or "", self.max_input_bytes
        )
        if not directory.name.startswith(f"tessera-input-{manifest_digest}-"):
            raise CpuError("Input directory is not the materialized frozen snapshot.")
        actual_files: set[str] = set()
        actual_directories: set[str] = set()
        aliases: dict[str, str] = {}

        def check_alias(path: str) -> None:
            alias = unicodedata.normalize("NFC", path).casefold()
            if alias in aliases and aliases[alias] != path:
                raise CpuError("Input snapshot contains filesystem aliases.")
            aliases[alias] = path

        def walk(current: Path, relative_path: str) -> None:
            try:
                current_info = current.lstat()
            except OSError as error:
                raise CpuError("Input snapshot changed while being verified.") from error
            if (
                stat.S_ISLNK(current_info.st_mode)
                or not stat.S_ISDIR(current_info.st_mode)
                or stat.S_IMODE(current_info.st_mode) != 0o555
            ):
                raise CpuError("Input snapshot contains unsafe filesystem nodes.")
            with os.scandir(current) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    rel = f"{relative_path}/{entry.name}" if relative_path else entry.name
                    check_alias(rel)
                    try:
                        info = path.lstat()
                    except OSError as error:
                        raise CpuError("Input snapshot changed while being verified.") from error
                    if stat.S_ISLNK(info.st_mode):
                        raise CpuError("Input snapshot contains unsafe filesystem nodes.")
                    if stat.S_ISDIR(info.st_mode):
                        if rel not in expected_directories:
                            raise CpuError("Input snapshot contains an unexpected directory.")
                        actual_directories.add(rel)
                        walk(path, rel)
                        continue
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise CpuError("Input snapshot contains unsafe filesystem nodes.")
                    manifest_entry = expected.get(rel)
                    if (
                        manifest_entry is None
                        or stat.S_IMODE(info.st_mode) != 0o444
                        or info.st_size != manifest_entry["size"]
                    ):
                        raise CpuError("Input snapshot no longer matches its manifest.")
                    try:
                        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                        with os.fdopen(descriptor, "rb") as source:
                            opened = os.fstat(source.fileno())
                            if opened.st_size != manifest_entry["size"]:
                                raise CpuError("Input snapshot size changed while being verified.")
                            # Hash in bounded pieces: never hold more than one chunk.
                            hasher = hashlib.sha256()
                            read_total = 0
                            while True:
                                piece = source.read(artifact_limits_module.CHUNK_BYTES)
                                if not piece:
                                    break
                                read_total += len(piece)
                                if read_total > manifest_entry["size"]:
                                    break
                                hasher.update(piece)
                            digest = (
                                hasher.hexdigest() if read_total == manifest_entry["size"] else ""
                            )
                    except OSError as error:
                        raise CpuError("Input snapshot changed while being verified.") from error
                    if (
                        opened.st_dev != info.st_dev
                        or opened.st_ino != info.st_ino
                        or opened.st_size != manifest_entry["size"]
                        or digest != manifest_entry["sha256"]
                    ):
                        raise CpuError("Input snapshot no longer matches its manifest.")
                    actual_files.add(rel)

        # Verify each member immediately before Docker receives the bind source,
        # including empty directories and names that collide on case-insensitive
        # filesystems.
        walk(directory, "")
        if actual_files != set(expected) or actual_directories != expected_directories:
            raise CpuError("Input snapshot file set is incomplete or has extra files.")
        return ["--mount", f"type=bind,src={directory},dst=/inputs,readonly"]

    def _development_output_mount(self, operation: CpuOperation) -> list[str]:
        mount = operation.development_output
        if mount is None:
            return []
        if self.development_output_root is None:
            raise CpuError("Development output profile is not configured on this adapter.")
        root = self.development_output_root
        directory = Path(mount.directory)
        try:
            if not directory.is_absolute() or directory == root:
                raise ValueError
            relative = directory.relative_to(root)
            current = root
            for part in relative.parts:
                current /= part
                if stat.S_ISLNK(current.lstat().st_mode):
                    raise ValueError
            info = directory.stat()
            resolved = directory.resolve(strict=True)
        except (OSError, ValueError) as error:
            raise CpuError("Development output workspace escapes supervisor custody.") from error
        if (
            resolved != directory
            or not stat.S_ISDIR(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise CpuError("Development output workspace is not isolated private custody.")
        return ["--mount", f"type=bind,src={directory},dst=/outputs"]

    def scratch_for(self, requested: int | None) -> int | None:
        """The scratch an operation runs with: the operator's default, or a plan's request
        within the operator ceiling (ADR 0059). Raises ``ValueError`` with the reason when
        the request cannot be granted, before any container exists."""
        if requested is None:
            return self.scratch_bytes
        if not limits.valid_bytes(requested, limits.MIN_SCRATCH_BYTES, limits.MAX_BYTES):
            raise ValueError("The requested scratch is outside the supported range.")
        if requested > self.max_scratch_bytes:
            raise ValueError(
                f"The requested scratch of {requested} bytes exceeds the operator ceiling of "
                f"{self.max_scratch_bytes} bytes."
            )
        if not limits.scratch_fits_memory(requested, self.memory_bytes):
            memory = self.memory_bytes or limits.DEFAULT_MEMORY_BYTES
            raise ValueError(
                f"The requested scratch of {requested} bytes needs more memory than the "
                f"operator's limit of {memory} bytes (scratch is memory-backed)."
            )
        return requested

    def operation_limits(self) -> dict[str, int]:
        """The operator limits to set on every CpuOperation this adapter will run."""
        values = {
            "memory_bytes": self.memory_bytes,
            "scratch_bytes": self.scratch_bytes,
            "container_user": self.container_user,
        }
        return {name: value for name, value in values.items() if value is not None}

    def create_arguments(self, operation: CpuOperation) -> list[str]:
        # The operation carries the limits (built from this adapter's operator limits by the
        # runtime), so the durable fence records exactly what runs; refuse any disagreement.
        if (
            (self.memory_bytes is not None and operation.memory_bytes != self.memory_bytes)
            or operation.scratch_bytes > self.max_scratch_bytes
            or (self.container_user is not None and operation.container_user != self.container_user)
        ):
            raise CpuError("Operation limits differ from the operator's container limits.")
        try:
            container_user = validate_container_user(
                operation.container_user, allow_root=self.allow_root_container_user
            )
        except ValueError as error:
            raise CpuError("Container user is not permitted by the operator.") from error
        memory_bytes, scratch_bytes = operation.memory_bytes, operation.scratch_bytes
        if operation.image not in self.allowed_images:
            raise CpuError("Image is outside the admitted execution profile.")
        if operation.network_profile == EGRESS_PROFILE:
            if self.egress_proxy_image is None:
                raise CpuError("Egress requires a configured egress proxy image.")
            network = ["--network", operation.egress_network_name]
            proxy = f"http://egress-proxy:{PROXY_PORT}"
            environment = [
                "--env",
                f"HTTPS_PROXY={proxy}",
                "--env",
                f"HTTP_PROXY={proxy}",
                "--env",
                "NO_PROXY=",
            ]
        else:
            network = ["--network", "none"]
            environment = []
        return [
            "create",
            "--pull",
            "never",
            "--restart",
            "no",
            "--no-healthcheck",
            "--name",
            operation.container_name,
            "--label",
            f"tessera.operation={operation.operation_id}",
            "--label",
            f"tessera.plan={operation.plan_digest}",
            *network,
            *environment,
            "--read-only",
            "--cpus",
            "1",
            "--memory",
            str(memory_bytes),
            "--memory-swap",
            str(memory_bytes),
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            container_user,
            "--workdir",
            "/work",
            *self._input_mount(operation),
            *self._development_output_mount(operation),
            "--tmpfs",
            f"/work:rw,noexec,nosuid,nodev,size={scratch_bytes},mode=1777",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=2",
            "--entrypoint",
            operation.argv[0],
            operation.image,
            *operation.argv[1:],
        ]

    def observe(self, operation: CpuOperation) -> CpuObservation:
        try:
            reply = self.run(["inspect", "--type", "container", operation.container_name])
        except CpuError:
            return CpuObservation("unknown")
        if reply.returncode:
            # A stopped daemon or authorization failure is not absence.
            if "No such container" in reply.stderr or "No such object" in reply.stderr:
                return CpuObservation("absent")
            return CpuObservation("unknown")
        try:
            items = json.loads(reply.stdout)
            if not isinstance(items, list) or len(items) != 1:
                raise ValueError
            info = items[0]
            labels = info["Config"]["Labels"]
            if (
                labels.get("tessera.operation") != operation.operation_id
                or labels.get("tessera.plan") != operation.plan_digest
            ):
                raise CpuError("Container ownership does not match the operation journal.")
            state = info["State"]
            status = state["Status"]
            if status == "running":
                return CpuObservation("running", info["Id"])
            if status == "created":
                return CpuObservation("created", info["Id"])
            if status in {"exited", "dead"} and state.get("Running") is False:
                return CpuObservation("exited", info["Id"], state.get("ExitCode"), True)
            return CpuObservation("unknown", info["Id"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return CpuObservation("unknown")

    def prepare(self, operation: CpuOperation) -> CpuObservation:
        arguments = self.create_arguments(operation)
        existing = self.observe(operation)
        if existing.state != "absent":
            return existing
        self._ensure_egress(operation, before_container=True)
        try:
            created = self.run(arguments)
        except CpuError:
            created = None
        # Both successful and lost create replies are reconciled by stable name.
        observed = self.observe(operation)
        if created is not None and created.returncode and observed.state == "absent":
            # A definite refusal and a fresh read of no container: nothing was launched.
            raise CpuLaunchError(
                "Docker refused to create the container.",
                launch_error_class(created.stderr, network=False),
            )
        return observed

    def start(self, operation: CpuOperation) -> CpuObservation:
        self.create_arguments(operation)  # Revalidate the configured profile.
        existing = self.observe(operation)
        if existing.state != "created":
            # In particular: never restart an exited experiment on redelivery.
            return existing
        self._ensure_egress(operation)
        try:
            self.run(["start", operation.container_name])
        except CpuError:
            pass
        return self.observe(operation)

    def cancel(self, operation: CpuOperation) -> CpuObservation:
        existing = self.observe(operation)
        if existing.state in {"absent", "unknown", "exited"}:
            if existing.state in {"absent", "exited"}:
                self.teardown_egress(operation)
            return existing
        if existing.state == "created":
            # No user code started, but retain the container as operation evidence.
            self.teardown_egress(operation)
            return CpuObservation("created", existing.container_id, termination_confirmed=True)
        try:
            self.run(["kill", "--signal", "KILL", operation.container_name])
        except CpuError:
            pass
        observed = self.observe(operation)
        if observed.state == "exited":
            self.teardown_egress(operation)
        return observed

    # --- governed egress (ADR 0034) -------------------------------------------------

    def _egress_directory(self, operation: CpuOperation) -> Path:
        if self.egress_root is None:
            raise CpuError("Egress journal root is not configured.")
        return self.egress_root / f"tessera-egress-{operation.operation_id}"

    def egress_journal_path(self, operation: CpuOperation) -> Path:
        return self._egress_directory(operation) / "journal" / "journal.jsonl"

    def egress_summary(self, operation: CpuOperation) -> tuple[str, dict[str, Any]]:
        """Return (journal sha256, bounded summary) of the kept journal."""
        path = self.egress_journal_path(operation)
        try:
            return journal_sha256(path), summarize_journal(path)
        except OSError as error:
            raise CpuError("Egress journal is unavailable.") from error

    def proxy_create_arguments(self, operation: CpuOperation) -> list[str]:
        if self.egress_proxy_image is None:
            raise CpuError("Egress requires a configured egress proxy image.")
        directory = self._egress_directory(operation)
        return [
            "create",
            "--pull",
            "never",
            "--restart",
            "no",
            "--no-healthcheck",
            "--name",
            operation.egress_proxy_name,
            "--label",
            f"tessera.operation={operation.operation_id}",
            "--label",
            "tessera.role=egress-proxy",
            "--network",
            operation.egress_network_name,
            "--network-alias",
            "egress-proxy",
            "--read-only",
            "--cpus",
            "1",
            "--memory",
            str(128 * 1024 * 1024),
            "--memory-swap",
            str(128 * 1024 * 1024),
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            "65532:65532",
            "--mount",
            f"type=bind,src={self.egress_proxy_script},dst=/opt/egress_proxy.py,readonly",
            "--mount",
            f"type=bind,src={directory / 'config.json'},dst=/etc/egress.json,readonly",
            "--mount",
            f"type=bind,src={directory / 'journal'},dst=/journal",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=2",
            "--entrypoint",
            "python",
            self.egress_proxy_image,
            "/opt/egress_proxy.py",
            "--config",
            "/etc/egress.json",
            "--journal",
            "/journal/journal.jsonl",
            "--listen",
            f"0.0.0.0:{PROXY_PORT}",
        ]

    def _stage_egress_files(self, operation: CpuOperation) -> None:
        assert operation.egress is not None
        if self.egress_root is None:
            raise CpuError("Egress journal root is not configured.")
        self._private_input_root(self.egress_root)
        directory = self._egress_directory(operation)
        journal_dir = directory / "journal"
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            journal_dir.mkdir(mode=0o777, exist_ok=True)
            os.chmod(journal_dir, 0o777)  # The non-root proxy user must append here.
            journal = journal_dir / "journal.jsonl"
            fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o666)
            os.close(fd)
            os.chmod(journal, 0o666)
            config = {
                "destinations": [
                    {"host": item["host"], "port": item["port"]}
                    for item in operation.egress["destinations"]
                ],
                "max_egress_bytes": operation.egress["max_egress_bytes"],
            }
            target = directory / "config.json"
            temporary = directory / "config.json.tmp"
            temporary.write_text(json.dumps(config, sort_keys=True))
            os.replace(temporary, target)
            os.chmod(target, 0o444)
            if not self.egress_proxy_script.is_file():
                raise CpuError("Egress proxy script is unavailable.")
        except OSError as error:
            raise CpuError("Egress staging failed.") from error

    def _checked(self, arguments: Sequence[str], *, tolerate: tuple[str, ...] = ()) -> None:
        reply = self.run(arguments)
        if reply.returncode and not any(text in reply.stderr for text in tolerate):
            raise CpuError("Docker refused an egress setup step.")

    def _ensure_egress(self, operation: CpuOperation, *, before_container: bool = False) -> None:
        """Idempotently create the internal network and a running proxy, or raise.

        ``before_container`` is true only when the stage container does not exist yet: a
        refused network create then raises ``CpuLaunchError``."""
        if operation.network_profile != EGRESS_PROFILE:
            return
        if self.egress_proxy_image is None:
            raise CpuError("Egress requires a configured egress proxy image.")
        self._stage_egress_files(operation)
        network = operation.egress_network_name
        proxy = operation.egress_proxy_name
        if self.run(["network", "inspect", network]).returncode:
            if before_container:
                # A refusal raises NetworkCreateError, a CpuLaunchError (ADR 0046).
                self._create_network(operation, network)
            else:
                try:
                    self._create_network(operation, network)
                except NetworkCreateError as error:
                    # A container may exist: never a launch error, only unconfirmed.
                    raise CpuError("Docker refused an egress setup step.") from error
        if self.run(["inspect", "--type", "container", proxy]).returncode:
            self._checked(self.proxy_create_arguments(operation), tolerate=("already in use",))
        self._checked(["start", proxy])
        self._checked(["network", "connect", "bridge", proxy], tolerate=("already exists",))

    def _create_network(self, operation: CpuOperation, network: str) -> None:
        if self.network_pool is not None:
            # Raises NetworkCreateError with Docker's message; no container exists yet.
            self.network_pool.create_network(self.run, network, operation.operation_id)
            return
        arguments = [
            "network",
            "create",
            "--internal",
            "--label",
            f"tessera.operation={operation.operation_id}",
            network,
        ]
        reply = self.run(arguments)
        if reply.returncode and "already exists" not in reply.stderr:
            detail = " ".join((reply.stderr or reply.stdout).split())[:400]
            raise NetworkCreateError(f"Docker refused to create network: {detail}", detail)

    def prune_orphan_networks(self) -> list[str]:
        """Remove Tessera-labelled networks with no container; a no-op without a pool."""
        if self.network_pool is None:
            return []
        return self.network_pool.prune_orphans(self.run)

    def teardown_egress(self, operation: CpuOperation) -> None:
        """Stop and remove the proxy and network; absent resources are tolerated."""
        if operation.network_profile != EGRESS_PROFILE:
            return
        for arguments in (
            ["stop", "--time", "2", operation.egress_proxy_name],
            ["rm", "--force", operation.egress_proxy_name],
            ["network", "rm", operation.egress_network_name],
        ):
            try:
                self.run(arguments)  # Failures leave resources for the next idempotent call.
            except CpuError:
                pass

    @staticmethod
    def cost_observation() -> dict[str, Any]:
        # Local CPU infrastructure costs are not measured by Docker exit status.
        return {
            "model_charge_cents": 0,
            "infrastructure_cost_cents": None,
            "infrastructure_cost_status": "unmeasured",
        }
