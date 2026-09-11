"""Narrow, human-approved local Minikube bootstrap for one disposable profile.

This is intentionally not a Kubernetes execution adapter or credential path.  It
uses Minikube's profile-scoped ``kubectl`` wrapper exclusively, so it cannot read,
switch, or rely on an ambient kubeconfig context.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import subprocess
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import canonical_json

PLAN_VERSION = "local-minikube-provisioning-v1"
PROFILE = "tessera-dev"
NAMESPACE = "tessera-dev"
PVC = "tessera-output"
MAX_OUTPUT_BYTES = 8_192
MINIKUBE_TIMEOUT_SECONDS = 300
KUBECTL_TIMEOUT_SECONDS = 20
RESULT_MAX_BYTES = 16_384
RESULT_TEMPORARY_ATTEMPTS = 8


class MinikubeProvisioningError(RuntimeError):
    """A live operation cannot be safely continued."""


@dataclass
class ResultJournal:
    """An opened, private parent directory for one no-clobber result publication."""

    directory_fd: int
    name: str

    def close(self) -> None:
        if self.directory_fd >= 0:
            os.close(self.directory_fd)
            self.directory_fd = -1

    def __enter__(self) -> ResultJournal:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class MinikubePlan:
    """The one supported disposable local-development plan."""

    version: str
    profile: str
    driver: str
    cpus: int
    memory_mib: int
    disk_mib: int
    namespace: str
    pvc: str
    pvc_storage: str
    retain_resources: bool
    digest: str

    @classmethod
    def default(cls) -> MinikubePlan:
        values: dict[str, object] = {
            "version": PLAN_VERSION,
            "profile": PROFILE,
            "driver": "docker",
            "cpus": 2,
            "memory_mib": 4096,
            "disk_mib": 20_480,
            "namespace": NAMESPACE,
            "pvc": PVC,
            "pvc_storage": "1Gi",
            "retain_resources": True,
        }
        return cls(**values, digest=_digest(values))  # type: ignore[arg-type]

    def payload(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if key != "digest"}

    def assert_immutable(self) -> None:
        if self != self.default() or self.digest != _digest(self.payload()):
            raise MinikubeProvisioningError("Only the immutable tessera-dev plan is supported.")


@dataclass(frozen=True)
class CommandResult:
    """Bounded local command output used only for immediate validation."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[..., CommandResult]


def _digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _minikube(*arguments: str) -> tuple[str, ...]:
    return ("minikube", *arguments)


def _kubectl(*arguments: str) -> tuple[str, ...]:
    return ("minikube", "--profile", PROFILE, "kubectl", "--", *arguments)


def pvc_manifest() -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {"name": PVC, "namespace": NAMESPACE},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "1Gi"}},
        },
    }


def namespace_manifest() -> dict[str, object]:
    return {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}


def planned_commands() -> tuple[tuple[str, ...], ...]:
    """Commands shown by dry run; manifest commands receive canonical JSON on stdin."""
    return (
        _minikube("profile", "list", "--output=json"),
        _minikube(
            "start",
            "--profile",
            PROFILE,
            "--driver=docker",
            "--cpus=2",
            "--memory=4096mb",
            "--disk-size=20480mb",
        ),
        _minikube("status", "--profile", PROFILE, "--output=json"),
        _kubectl("--request-timeout=10s", "get", "namespace", "kube-system", "-o", "json"),
        _kubectl("--request-timeout=10s", "get", "namespace", NAMESPACE, "-o", "json"),
        _kubectl("--request-timeout=10s", "create", "-f", "-"),
        _kubectl(
            "--request-timeout=10s", "get", "pvc", PVC, "--namespace", NAMESPACE, "-o", "json"
        ),
        _kubectl("--request-timeout=10s", "create", "-f", "-"),
    )


def render_plan(plan: MinikubePlan) -> str:
    plan.assert_immutable()
    lines = [
        "Tessera local Minikube provisioning plan (dry run)",
        f"plan digest: {plan.digest}",
        "human approval: required; an agent, research approval, worker, or skill cannot approve",
        "mutation: disabled; rerun with --apply and --approved-plan <this digest>",
        "retention: profile, namespace, and PVC are retained; teardown is not implemented",
        "commands:",
    ]
    for command in planned_commands():
        has_manifest_stdin = command[-3:] == ("-f", "-")
        suffix = "  # canonical Namespace/PVC manifest on stdin" if has_manifest_stdin else ""
        lines.append("  " + " ".join(command) + suffix)
    return "\n".join(lines)


def _bounded_runner(
    argv: tuple[str, ...], *, input: str | None = None, timeout: int
) -> CommandResult:
    """Run one argv without a shell, bounded by deadline and output size."""
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if input is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise MinikubeProvisioningError("Required local command could not be started.") from error
    assert process.stdout is not None and process.stderr is not None
    captured: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    overflow = threading.Event()

    def drain(name: str, stream: Any) -> None:
        while chunk := stream.read(1024):
            room = MAX_OUTPUT_BYTES - len(captured[name])
            captured[name].extend(chunk[: max(room, 0)])
            if len(chunk) > room:
                overflow.set()
                process.kill()
                return

    threads = [
        threading.Thread(target=drain, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=drain, args=("stderr", process.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()
    try:
        if input is not None:
            assert process.stdin is not None
            process.stdin.write(input.encode("utf-8"))
            process.stdin.close()
        process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        process.kill()
        process.wait()
        raise MinikubeProvisioningError(
            "Local command timed out or could not be completed."
        ) from error
    finally:
        for thread in threads:
            thread.join(timeout=1)
    if overflow.is_set():
        raise MinikubeProvisioningError("Local command exceeded the bounded output limit.")
    return CommandResult(
        argv=argv,
        returncode=process.returncode,
        stdout=bytes(captured["stdout"]).decode("utf-8", "replace"),
        stderr=bytes(captured["stderr"]).decode("utf-8", "replace"),
    )


def _json(result: CommandResult, subject: str) -> dict[str, Any]:
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise MinikubeProvisioningError(f"{subject} returned malformed data.") from error
    if not isinstance(value, dict):
        raise MinikubeProvisioningError(f"{subject} returned malformed data.")
    return value


def _failure_category(result: CommandResult) -> str:
    message = result.stderr.lower()
    if "forbidden" in message or "unauthorized" in message:
        return "authentication_or_authorization_failure"
    if "connect" in message or "timeout" in message:
        return "connectivity_failure"
    if "notfound" in message or "not found" in message:
        return "not_found"
    return "subprocess_failure"


class MinikubeProvisioner:
    """Reconciles only ``tessera-dev`` through profile-scoped Minikube commands."""

    def __init__(self, runner: Runner = _bounded_runner) -> None:
        self._runner = runner

    def _run(
        self, argv: tuple[str, ...], *, timeout: int, input: str | None = None
    ) -> CommandResult:
        try:
            result = self._runner(argv, input=input, timeout=timeout)
        except MinikubeProvisioningError:
            raise
        except (OSError, subprocess.SubprocessError) as error:
            raise MinikubeProvisioningError(
                "Local command failed with uncertain effects."
            ) from error
        if result.returncode != 0:
            raise MinikubeProvisioningError(
                f"Local command failed: {_failure_category(result)}; effects are not assumed."
            )
        return result

    def _get(
        self, resource: str, name: str, *, namespace: str | None = None
    ) -> dict[str, Any] | None:
        command = ["--request-timeout=10s", "get", resource, name]
        if namespace is not None:
            command.extend(("--namespace", namespace))
        command.extend(("-o", "json"))
        try:
            result = self._run(_kubectl(*command), timeout=KUBECTL_TIMEOUT_SECONDS)
        except MinikubeProvisioningError as error:
            if "not_found" in str(error):
                return None
            raise
        return _json(result, resource)

    @staticmethod
    def _identity(value: dict[str, Any], kind: str, name: str, namespace: str | None = None) -> str:
        metadata = value.get("metadata")
        if value.get("kind") != kind or not isinstance(metadata, dict):
            raise MinikubeProvisioningError(f"{kind} identity did not match the approved target.")
        if metadata.get("name") != name or (
            namespace is not None and metadata.get("namespace") != namespace
        ):
            raise MinikubeProvisioningError(f"{kind} identity did not match the approved target.")
        uid = metadata.get("uid")
        if not isinstance(uid, str) or not uid:
            raise MinikubeProvisioningError(f"{kind} identity did not include a UID.")
        return uid

    def _verify_cluster(self) -> str:
        observed = self._get("namespace", "kube-system")
        if observed is None:
            raise MinikubeProvisioningError("Minikube cluster identity was unavailable.")
        return self._identity(observed, "Namespace", "kube-system")

    def _profile_exists_and_matches(self) -> bool:
        listing = _json(
            self._run(
                _minikube("profile", "list", "--output=json"), timeout=KUBECTL_TIMEOUT_SECONDS
            ),
            "Minikube profile list",
        )
        profiles = listing.get("valid")
        if not isinstance(profiles, list):
            raise MinikubeProvisioningError("Minikube profile list returned malformed data.")
        matches = [
            entry for entry in profiles if isinstance(entry, dict) and entry.get("Name") == PROFILE
        ]
        if not matches:
            return False
        if len(matches) != 1:
            raise MinikubeProvisioningError("Minikube profile identity is ambiguous.")
        config = matches[0].get("Config")
        if not isinstance(config, dict) or not _profile_config_matches(config):
            raise MinikubeProvisioningError(
                "Existing Minikube profile does not match the approved plan."
            )
        return True

    def _verify_running_profile(self) -> None:
        status = _json(
            self._run(
                _minikube("status", "--profile", PROFILE, "--output=json"),
                timeout=KUBECTL_TIMEOUT_SECONDS,
            ),
            "Minikube status",
        )
        if status.get("Host") != "Running" or status.get("APIServer") != "Running":
            raise MinikubeProvisioningError("Minikube profile is not a running approved target.")

    def status(self, plan: MinikubePlan) -> dict[str, object]:
        plan.assert_immutable()
        if not self._profile_exists_and_matches():
            return {"state": "absent", "profile": PROFILE, "plan_digest": plan.digest}
        self._verify_running_profile()
        cluster_uid = self._verify_cluster()
        namespace = self._get("namespace", NAMESPACE)
        if namespace is None:
            return {
                "state": "cluster_ready",
                "cluster_uid": cluster_uid,
                "plan_digest": plan.digest,
            }
        namespace_uid = self._identity(namespace, "Namespace", NAMESPACE)
        pvc = self._get("pvc", PVC, namespace=NAMESPACE)
        if pvc is None:
            return {
                "state": "namespace_ready",
                "cluster_uid": cluster_uid,
                "namespace_uid": namespace_uid,
                "plan_digest": plan.digest,
            }
        pvc_uid = _verify_pvc(pvc)
        return {
            "state": "ready",
            "cluster_uid": cluster_uid,
            "namespace_uid": namespace_uid,
            "pvc_uid": pvc_uid,
            "plan_digest": plan.digest,
            "retained": True,
        }

    def apply(self, plan: MinikubePlan, approved_plan_digest: str) -> dict[str, object]:
        plan.assert_immutable()
        if approved_plan_digest != plan.digest:
            raise MinikubeProvisioningError(
                "Human approval must name this exact immutable plan digest."
            )
        profile_existed = self._profile_exists_and_matches()
        if not profile_existed:
            self._run(planned_commands()[1], timeout=MINIKUBE_TIMEOUT_SECONDS)
            if not self._profile_exists_and_matches():
                raise MinikubeProvisioningError(
                    "Minikube did not report the approved profile after start."
                )
        self._verify_running_profile()
        cluster_uid = self._verify_cluster()

        namespace = self._get("namespace", NAMESPACE)
        if namespace is None:
            self._run(
                _kubectl("--request-timeout=10s", "create", "-f", "-"),
                timeout=KUBECTL_TIMEOUT_SECONDS,
                input=canonical_json(namespace_manifest()).decode("utf-8"),
            )
            namespace = self._get("namespace", NAMESPACE)
        if namespace is None:
            raise MinikubeProvisioningError("Approved namespace was not observable after creation.")
        namespace_uid = self._identity(namespace, "Namespace", NAMESPACE)
        cluster_uid = self._verify_cluster()

        pvc = self._get("pvc", PVC, namespace=NAMESPACE)
        if pvc is None:
            self._run(
                _kubectl("--request-timeout=10s", "create", "-f", "-"),
                timeout=KUBECTL_TIMEOUT_SECONDS,
                input=canonical_json(pvc_manifest()).decode("utf-8"),
            )
            pvc = self._get("pvc", PVC, namespace=NAMESPACE)
        if pvc is None:
            raise MinikubeProvisioningError("Approved PVC was not observable after creation.")
        pvc_uid = _verify_pvc(pvc)
        cluster_uid = self._verify_cluster()
        namespace_uid = self._identity(
            self._get("namespace", NAMESPACE) or {}, "Namespace", NAMESPACE
        )
        return {
            "state": "reconciled",
            "plan_digest": plan.digest,
            "profile_created": not profile_existed,
            "cluster_uid": cluster_uid,
            "namespace_uid": namespace_uid,
            "pvc_uid": pvc_uid,
            "retained": True,
        }


def _quantity_mib(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    suffixes = {"mb": 1, "mib": 1, "gib": 1024}
    for suffix, multiplier in suffixes.items():
        if normalized.endswith(suffix) and normalized[: -len(suffix)].isdigit():
            return int(normalized[: -len(suffix)]) * multiplier
    return int(normalized) if normalized.isdigit() else None


def _profile_config_matches(config: dict[str, Any]) -> bool:
    driver = config.get("Driver", config.get("VMDriver"))
    return (
        driver == "docker"
        and _quantity_mib(config.get("CPUs")) == 2
        and _quantity_mib(config.get("Memory")) == 4096
        and _quantity_mib(config.get("DiskSize")) == 20_480
    )


def _verify_pvc(value: dict[str, Any]) -> str:
    uid = MinikubeProvisioner._identity(value, "PersistentVolumeClaim", PVC, NAMESPACE)
    spec = value.get("spec")
    if not isinstance(spec, dict):
        raise MinikubeProvisioningError("PVC specification did not match the approved plan.")
    if spec.get("accessModes") != ["ReadWriteOnce"]:
        raise MinikubeProvisioningError("PVC specification did not match the approved plan.")
    resources = spec.get("resources")
    requests = resources.get("requests") if isinstance(resources, dict) else None
    if not isinstance(requests, dict) or requests.get("storage") != "1Gi":
        raise MinikubeProvisioningError("PVC specification did not match the approved plan.")
    return uid


def prepare_result_journal(path: Path) -> ResultJournal:
    """Open a private, existing journal parent without following its final component.

    Call this before an infrastructure mutation. The retained directory descriptor
    avoids re-resolving a path after that check; publication itself remains
    no-clobber so a concurrent writer cannot replace another operation's record.
    """
    if os.name != "posix" or not all(
        hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW", "O_EXCL")
    ):
        raise MinikubeProvisioningError("Safe operation-result storage is unavailable.")
    target = Path(path)
    if target.name in {"", ".", ".."}:
        raise MinikubeProvisioningError("Operation journal destination is unavailable.")
    try:
        directory_fd = os.open(
            target.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )
    except OSError as error:
        raise MinikubeProvisioningError("Operation journal parent is not private.") from error
    try:
        directory = os.fstat(directory_fd)
        if (
            not stat.S_ISDIR(directory.st_mode)
            or directory.st_uid != os.geteuid()
            or stat.S_IMODE(directory.st_mode) != 0o700
        ):
            raise MinikubeProvisioningError("Operation journal parent is not private.")
        try:
            os.stat(target.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return ResultJournal(directory_fd, target.name)
        raise MinikubeProvisioningError("Operation journal destination is unavailable.")
    except OSError as error:
        os.close(directory_fd)
        raise MinikubeProvisioningError("Operation journal destination is unavailable.") from error
    except Exception:
        os.close(directory_fd)
        raise


def _temporary_result_name(name: str) -> str:
    return f".{name}.{secrets.token_hex(16)}.tmp"


def _write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        if written == 0:
            raise OSError("short operation-result write")
        remaining = remaining[written:]


def _record_result(journal: ResultJournal, encoded: bytes) -> None:
    temporary: str | None = None
    descriptor = -1
    try:
        for _ in range(RESULT_TEMPORARY_ATTEMPTS):
            candidate = _temporary_result_name(journal.name)
            try:
                descriptor = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK,
                    0o600,
                    dir_fd=journal.directory_fd,
                )
            except FileExistsError:
                continue
            temporary = candidate
            break
        if temporary is None:
            raise OSError("operation-result temporary name collision")
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise OSError("unsafe operation-result temporary file")
        _write_all(descriptor, encoded)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("unsafe operation-result temporary file")
        os.close(descriptor)
        descriptor = -1
        os.link(
            temporary,
            journal.name,
            src_dir_fd=journal.directory_fd,
            dst_dir_fd=journal.directory_fd,
            follow_symlinks=False,
        )
        os.fsync(journal.directory_fd)
    except OSError as error:
        raise MinikubeProvisioningError(
            "Sanitized operation result could not be recorded."
        ) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary, dir_fd=journal.directory_fd)
            except FileNotFoundError:
                pass
            except OSError:
                pass


def record_result(path: Path | ResultJournal, result: dict[str, object]) -> None:
    """No-clobber retain a bounded, secret-free local operation result."""
    safe = {"recorded_at": _utc_now(), **result}
    encoded = canonical_json(safe)
    if len(encoded) > RESULT_MAX_BYTES:
        raise MinikubeProvisioningError("Operation result exceeded its safe storage bound.")
    if isinstance(path, ResultJournal):
        _record_result(path, encoded)
        return
    with prepare_result_journal(path) as journal:
        _record_result(journal, encoded)


__all__ = [
    "CommandResult",
    "MinikubePlan",
    "MinikubeProvisioner",
    "MinikubeProvisioningError",
    "NAMESPACE",
    "PROFILE",
    "PVC",
    "ResultJournal",
    "planned_commands",
    "prepare_result_journal",
    "record_result",
    "render_plan",
]
