"""Docker CPU boundary with stable operation identity and explicit uncertainty.

Only the trusted supervisor may construct an operation from an active grant.
Only explicitly verified public inputs may be mounted, read-only, into an experiment.
Docker sockets, credentials and other host paths are never mounted.
This adapter does not infer scientific success from a container exit code.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

_IMAGE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9./:_-]*@sha256:[a-f0-9]{64}\Z")
_OPERATION = re.compile(r"[a-f0-9]{32}\Z")
MAX_CAPTURE_BYTES = 2 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_INPUT_FILES = 1000
MAX_INPUT_MANIFEST_BYTES = 1024 * 1024
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")


class CpuError(Exception):
    """A provider operation could not be confirmed."""


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
    memory_bytes: int = 256 * 1024 * 1024
    scratch_bytes: int = 16 * 1024 * 1024
    input_directory: str | None = None
    input_manifest_json: str | None = None
    development_output: DevelopmentOutputMount | None = None

    def __post_init__(self) -> None:
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
        if not 32 * 1024 * 1024 <= self.memory_bytes <= 1024 * 1024 * 1024:
            raise ValueError("Invalid memory bound.")
        if not 1024 * 1024 <= self.scratch_bytes <= 64 * 1024 * 1024:
            raise ValueError("Invalid scratch bound.")
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
    ):
        if not allowed_images or any(not _IMAGE.fullmatch(image) for image in allowed_images):
            raise ValueError("At least one pinned trusted image is required.")
        self.allowed_images = allowed_images
        self.run = run
        self.input_root = self._private_input_root(input_root) if input_root is not None else None
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
    def _manifest_entries(cls, encoded: str) -> tuple[dict[str, dict[str, Any]], set[str], str]:
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
                or not 0 <= manifest["total_bytes"] <= MAX_INPUT_BYTES
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
                    or not 0 <= entry["size"] <= MAX_INPUT_BYTES
                    or not isinstance(entry["sha256"], str)
                    or not _DIGEST.fullmatch(entry["sha256"])
                    or entry["provenance"] != "imported_unverified"
                    or entry["classification"] != "unclassified"
                ):
                    raise ValueError
                total += entry["size"]
                if total > MAX_INPUT_BYTES:
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
            operation.input_manifest_json or ""
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
                            content = source.read(manifest_entry["size"] + 1)
                            digest = hashlib.sha256(content).hexdigest()
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

    def create_arguments(self, operation: CpuOperation) -> list[str]:
        if operation.image not in self.allowed_images:
            raise CpuError("Image is outside the admitted execution profile.")
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
            "--network",
            "none",
            "--read-only",
            "--cpus",
            "1",
            "--memory",
            str(operation.memory_bytes),
            "--memory-swap",
            str(operation.memory_bytes),
            "--pids-limit",
            "64",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges=true",
            "--user",
            "65532:65532",
            "--workdir",
            "/work",
            *self._input_mount(operation),
            *self._development_output_mount(operation),
            "--tmpfs",
            f"/work:rw,noexec,nosuid,nodev,size={operation.scratch_bytes},mode=1777",
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
        try:
            self.run(arguments)
        except CpuError:
            pass
        # Both successful and lost create replies are reconciled by stable name.
        return self.observe(operation)

    def start(self, operation: CpuOperation) -> CpuObservation:
        self.create_arguments(operation)  # Revalidate the configured profile.
        existing = self.observe(operation)
        if existing.state != "created":
            # In particular: never restart an exited experiment on redelivery.
            return existing
        try:
            self.run(["start", operation.container_name])
        except CpuError:
            pass
        return self.observe(operation)

    def cancel(self, operation: CpuOperation) -> CpuObservation:
        existing = self.observe(operation)
        if existing.state in {"absent", "unknown", "exited"}:
            return existing
        if existing.state == "created":
            # No user code started, but retain the container as operation evidence.
            return CpuObservation("created", existing.container_id, termination_confirmed=True)
        try:
            self.run(["kill", "--signal", "KILL", operation.container_name])
        except CpuError:
            pass
        return self.observe(operation)

    @staticmethod
    def cost_observation() -> dict[str, Any]:
        # Local CPU infrastructure costs are not measured by Docker exit status.
        return {
            "model_charge_cents": 0,
            "infrastructure_cost_cents": None,
            "infrastructure_cost_status": "unmeasured",
        }
